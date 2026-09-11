import json
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_git import TaskGraphGit
from scripts.task_graph_verification import verify_task_commit
from scripts.task_graph_controller import TaskGraphController
from scripts.task_graph_runtime import create_run_snapshot, create_state, write_state, load_state, TaskGraphRuntimeError
from tests.test_task_graph_git import _git, _repo


class TaskGraphVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        _repo(self.root)
        self.git = TaskGraphGit(self.root)
        self.base = self.git.head_sha()
        self.worker = Path(self.temp.name) / 'worker'
        self.git.create_worker_worktree(self.worker, 'worker', self.base)
        (self.worker / 'result.txt').write_text('correct')
        _git(self.worker, 'add', 'result.txt')
        _git(self.worker, 'commit', '--quiet', '-m', 'task')
        self.commit = self.git.head_sha(self.worker)
        self.attempt = {'worktree': str(self.worker), 'launchBaseSha': self.base}
        self.task = {
            'predictedPaths': ['result.txt'],
            'verification': {'commands': [[sys.executable, '-c',
                "from pathlib import Path; assert Path('result.txt').read_text() == 'correct'"]]},
        }
        self.logs = Path(self.temp.name) / 'logs'

    def verify(self):
        return verify_task_commit(self.git, self.task, self.attempt, self.commit, self.logs)

    def test_real_committed_content_passes_with_recorded_command_evidence(self):
        evidence = self.verify()
        self.assertTrue(evidence['passed'], evidence)
        self.assertEqual(self.commit, evidence['commitSha'])
        self.assertEqual(0, evidence['commands'][0]['exitCode'])
        self.assertTrue(Path(evidence['commands'][0]['log']).is_file())
        self.assertTrue(self.git.is_clean(self.worker))

    def test_false_success_is_rejected_by_independent_command(self):
        self.task['verification']['commands'] = [[sys.executable, '-c', "print('failure evidence'); raise SystemExit(7)"]]
        evidence = self.verify()
        self.assertFalse(evidence['passed'])
        self.assertEqual(7, evidence['commands'][0]['exitCode'])
        self.assertIn('failure evidence', Path(evidence['commands'][0]['log']).read_text())

    def test_untracked_staged_and_unstaged_changes_are_rejected(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                dirty = self.worker / 'dirty.txt'
                dirty.write_text('do not discard')
                if staged:
                    _git(self.worker, 'add', 'dirty.txt')
                self.assertFalse(self.verify()['passed'])
                if staged:
                    _git(self.worker, 'reset', '--', 'dirty.txt')
                dirty.unlink()
        (self.worker / 'result.txt').write_text('dirty')
        self.assertFalse(self.verify()['passed'])

    def test_unplanned_path_and_rename_source_are_rejected(self):
        self.task['predictedPaths'] = ['other.txt']
        self.assertFalse(self.verify()['passed'])
        _git(self.worker, 'mv', 'baseline.txt', 'renamed.txt')
        _git(self.worker, 'commit', '--amend', '--no-edit', '--quiet')
        self.commit = self.git.head_sha(self.worker)
        self.task['predictedPaths'] = ['result.txt', 'renamed.txt']
        evidence = self.verify()
        self.assertFalse(evidence['passed'])
        self.assertIn('baseline.txt', evidence['reason'])

    def test_command_mutating_worktree_is_rejected(self):
        self.task['verification']['commands'] = [[sys.executable, '-c', "from pathlib import Path; Path('result.txt').write_text('changed')"]]
        self.assertFalse(self.verify()['passed'])

    def test_timeout_and_missing_executable_fail_closed(self):
        self.task['verification'] = {'commands': [[sys.executable, '-c', 'import time; time.sleep(30)']], 'timeoutSeconds': 1}
        evidence = self.verify()
        self.assertFalse(evidence['passed'])
        self.assertTrue(evidence['commands'][0]['timedOut'])
        self.task['verification'] = {'commands': [['/nonexistent/task-graph-check']]}
        self.assertFalse(self.verify()['passed'])

    def test_missing_verification_is_not_silently_skipped(self):
        del self.task['verification']
        self.assertFalse(self.verify()['passed'])

    def test_explicit_skip_is_recorded_but_still_enforces_scope(self):
        self.task['verification'] = {'skipReason': 'Documentation-only task; no automated check exists.'}
        evidence = self.verify()
        self.assertTrue(evidence['passed'], evidence)
        self.assertIn('Documentation', evidence['skipReason'])
        self.task['predictedPaths'] = []
        self.assertFalse(self.verify()['passed'])

    def test_commit_must_descend_from_launch_base(self):
        _git(self.worker, 'checkout', '--orphan', 'unrelated')
        _git(self.worker, 'commit', '--quiet', '-m', 'unrelated root')
        inspection = self.git.inspect_one_task_commit(self.worker, self.base)
        self.assertFalse(inspection.valid)

    def controller(self):
        plan = Path(self.temp.name) / 'plan'
        (plan / 'todo').mkdir(parents=True)
        (plan / 'todo/task.md').write_text('# Task\n\n## Dependencies\n\nNone\n')
        task = dict(self.task, id='task', taskFile='task.md', title='Task', instructions='Write result.',
                    predictedSymbols=[], dependsOn=[], parallelSafe=True, schedulingRationale='disjoint')
        (plan / 'dag.json').write_text(json.dumps({'schemaVersion': 1, 'planSlug': 'plan', 'tasks': [task]}))
        run = plan / 'runs/run-1'
        snapshot = create_run_snapshot(plan, run)
        integration = Path(self.temp.name) / 'integration'
        self.git.create_worker_worktree(integration, 'feature', self.base)
        state = create_state(run_id='run-1', plan_slug='plan', repository=str(self.root),
                             feature_branch='feature', base_commit=self.base, snapshot_digest=snapshot.dag_digest,
                             task_digests=snapshot.task_digests, max_workers=1, task_ids=['task'],
                             git_common_dir=str(self.git.common_dir()))
        exit_file = run / 'worker.exit'
        exit_file.write_text('0\n')
        state.update(integrationWorktree=str(integration))
        state['tasks']['task'].update(status='running', attempts=[dict(self.attempt, exitFile=str(exit_file))])
        write_state(run, state)
        return TaskGraphController(run), integration

    def test_controller_integrates_only_after_real_verification(self):
        controller, integration = self.controller()
        controller.poll_running_attempts()
        controller.integrate_waiting_tasks()
        saved = load_state(controller.run_dir)['tasks']['task']
        self.assertEqual('integrated', saved['status'])
        self.assertTrue(saved['attempts'][0]['verification']['passed'])
        self.assertEqual('correct', (integration / 'result.txt').read_text())

    def test_controller_rejects_worker_exit_zero_when_tests_fail(self):
        self.task['verification']['commands'] = [[sys.executable, '-c', 'raise SystemExit(9)']]
        controller, integration = self.controller()
        controller.poll_running_attempts()
        controller.integrate_waiting_tasks()
        saved = load_state(controller.run_dir)['tasks']['task']
        self.assertEqual('retrying', saved['status'])
        self.assertEqual(9, saved['attempts'][0]['verification']['commands'][0]['exitCode'])
        self.assertEqual(self.base, self.git.head_sha(integration))
        self.assertTrue(self.worker.exists())

    def test_controller_refuses_legacy_awaiting_commit_without_evidence(self):
        controller, integration = self.controller()
        controller.state['tasks']['task'].update(status='awaiting_integration', commitSha=self.commit)
        controller.integrate_waiting_tasks()
        self.assertEqual('retrying', controller.state['tasks']['task']['status'])
        self.assertEqual(self.base, self.git.head_sha(integration))

    def test_start_rejects_legacy_plan_before_writing_snapshot(self):
        del self.task['verification']
        with self.assertRaisesRegex(TaskGraphRuntimeError, 'verification'):
            self.controller()
        self.assertFalse((Path(self.temp.name) / 'plan/runs/run-1/input').exists())


if __name__ == '__main__':
    unittest.main()
