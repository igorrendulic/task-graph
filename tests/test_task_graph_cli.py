import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import ANY, patch

from scripts import task_graph_cli
from scripts.task_graph_cli import build_parser, controller_command
from scripts.task_graph_git import MergeResult, TaskGraphGitError
from scripts.task_graph_runtime import TaskGraphRuntimeError, create_state, load_state, write_state


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(root: Path) -> str:
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "baseline.txt").write_text("baseline")
    _git(root, "add", "baseline.txt")
    _git(root, "commit", "--quiet", "-m", "baseline")
    return _git(root, "branch", "--show-current")


class TaskGraphCliTests(unittest.TestCase):
    def _state(self, root: Path, run_id: str, status: str = "integrated") -> Path:
        run_dir = root / ".agent" / "demo-plan" / "runs" / run_id
        state = create_state(
            run_id=run_id,
            plan_slug="demo-plan",
            repository=str(root),
            feature_branch=f"task-graph/demo-plan/{run_id}/feature",
            base_commit="abc123",
            base_branch="main",
            snapshot_digest="digest",
            task_digests={"001-first": "task-digest"},
            max_workers=1,
            task_ids=["001-first"],
            git_common_dir=str(root / ".git"),
        )
        state["tasks"]["001-first"]["status"] = status
        write_state(run_dir, state)
        return run_dir

    def test_status_defaults_to_the_newest_run(self):
        with tempfile.TemporaryDirectory() as temp, patch("scripts.task_graph_cli._repository_root") as repository_root:
            root = Path(temp)
            self._state(root, "20260717T000000Z-old")
            self._state(root, "20260717T000001Z-new")
            repository_root.return_value = root

            self.assertEqual(
                "20260717T000001Z-new: succeeded",
                task_graph_cli.status("demo-plan"),
            )

    def test_status_reports_a_promoted_run_as_already_merged(self):
        with tempfile.TemporaryDirectory() as temp, patch("scripts.task_graph_cli._repository_root") as repository_root:
            root = Path(temp)
            run_dir = self._state(root, "run-1")
            state = load_state(run_dir)
            state["promotion"] = {
                "targetBranch": "main",
                "mergeSha": "merge-sha",
                "mergedAt": 1.0,
            }
            write_state(run_dir, state)
            repository_root.return_value = root

            self.assertEqual("run-1: already merged", task_graph_cli.status("demo-plan", "run-1"))

    def test_status_reports_running_and_failed_runs(self):
        with tempfile.TemporaryDirectory() as temp, patch("scripts.task_graph_cli._repository_root") as repository_root:
            root = Path(temp)
            self._state(root, "running", status="pending")
            self._state(root, "failed", status="failed")
            repository_root.return_value = root

            self.assertEqual("running: running", task_graph_cli.status("demo-plan", "running"))
            self.assertEqual("failed: failed", task_graph_cli.status("demo-plan", "failed"))

    def test_merge_rejects_a_run_that_is_not_fully_integrated(self):
        with tempfile.TemporaryDirectory() as temp, patch("scripts.task_graph_cli._repository_root") as repository_root:
            root = Path(temp)
            self._state(root, "run-1", status="failed")
            repository_root.return_value = root

            with self.assertRaisesRegex(TaskGraphRuntimeError, "all tasks are integrated"):
                task_graph_cli.merge("demo-plan", "run-1")

    @patch("scripts.task_graph_cli.TaskGraphGit")
    @patch("scripts.task_graph_cli._repository_root")
    def test_merge_rejects_the_wrong_checked_out_branch(self, repository_root, git_class):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._state(root, "run-1")
            repository_root.return_value = root
            git_class.return_value.current_branch.return_value = "other"

            with self.assertRaisesRegex(TaskGraphRuntimeError, "checked out branch"):
                task_graph_cli.merge("demo-plan", "run-1")

            git_class.return_value.merge_feature_branch.assert_not_called()

    @patch("scripts.task_graph_cli.TaskGraphGit")
    @patch("scripts.task_graph_cli._repository_root")
    def test_merge_rejects_a_dirty_checkout(self, repository_root, git_class):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._state(root, "run-1")
            repository_root.return_value = root
            git = git_class.return_value
            git.current_branch.return_value = "main"
            git.is_clean.return_value = False

            with self.assertRaisesRegex(TaskGraphRuntimeError, "repository is dirty"):
                task_graph_cli.merge("demo-plan", "run-1")

            git.merge_feature_branch.assert_not_called()

    @patch("scripts.task_graph_cli.TaskGraphGit")
    @patch("scripts.task_graph_cli._repository_root")
    def test_merge_persists_promotion_metadata(self, repository_root, git_class):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self._state(root, "run-1")
            repository_root.return_value = root
            git = git_class.return_value
            git.current_branch.return_value = "main"
            git.is_clean.return_value = True
            git.branch_exists.return_value = True
            git.merge_feature_branch.return_value = MergeResult("merged", "merge-sha")

            result = task_graph_cli.merge("demo-plan", "run-1")

            self.assertEqual("run-1: merged into main (merge-sha)", result)
            self.assertEqual(
                {"targetBranch": "main", "mergeSha": "merge-sha", "mergedAt": ANY},
                load_state(run_dir)["promotion"],
            )

    @patch("scripts.task_graph_cli.TaskGraphGit")
    @patch("scripts.task_graph_cli._repository_root")
    def test_merge_reports_a_previously_promoted_run_without_calling_git(self, repository_root, git_class):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self._state(root, "run-1")
            state = load_state(run_dir)
            state["promotion"] = {"targetBranch": "main", "mergeSha": "merge-sha", "mergedAt": 1.0}
            write_state(run_dir, state)
            repository_root.return_value = root

            self.assertEqual("run-1: already merged", task_graph_cli.merge("demo-plan", "run-1"))
            git_class.assert_not_called()

    def test_two_completed_runs_merge_sequentially_into_the_same_base_branch(self):
        with tempfile.TemporaryDirectory() as temp, patch("scripts.task_graph_cli._repository_root") as repository_root:
            root = Path(temp) / "repo"
            root.mkdir()
            base_branch = _repo(root)
            repository_root.return_value = root
            for run_id in ("run-1", "run-2"):
                feature_branch = f"task-graph/demo-plan/{run_id}/feature"
                feature_worktree = root.parent / run_id
                _git(root, "branch", feature_branch)
                _git(root, "worktree", "add", "--quiet", str(feature_worktree), feature_branch)
                (feature_worktree / f"{run_id}.txt").write_text(run_id)
                _git(feature_worktree, "add", f"{run_id}.txt")
                _git(feature_worktree, "commit", "--quiet", "-m", run_id)
                run_dir = self._state(root, run_id)
                state = load_state(run_dir)
                state["baseBranch"] = base_branch
                write_state(run_dir, state)

            self.assertIn("merged into", task_graph_cli.merge("demo-plan", "run-1"))
            self.assertIn("merged into", task_graph_cli.merge("demo-plan", "run-2"))
            self.assertTrue((root / "run-1.txt").is_file())
            self.assertTrue((root / "run-2.txt").is_file())

    @patch("scripts.task_graph_cli.notify_completion")
    def test_completion_alerts_include_the_follow_up_command(self, notify):
        task_graph_cli._notify_run_completion(
            {"planSlug": "demo-plan", "runId": "run-1", "tasks": {"001": {"status": "integrated"}}}
        )
        task_graph_cli._notify_run_completion(
            {"planSlug": "demo-plan", "runId": "run-2", "tasks": {"001": {"status": "failed"}}}
        )

        self.assertEqual(2, notify.call_count)
        self.assertIn("merge demo-plan --run-id run-1", notify.call_args_list[0].kwargs["message"])
        self.assertIn("status demo-plan --run-id run-2", notify.call_args_list[1].kwargs["message"])

    @patch("scripts.task_graph_cli.TerminalDashboard")
    @patch("scripts.task_graph_cli.TaskGraphController")
    def test_controller_cleans_up_dashboard_when_initial_draw_raises(
        self, controller_class, dashboard_class
    ):
        dashboard_class.return_value.start.side_effect = RuntimeError("terminal gone")

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "terminal gone"):
                task_graph_cli.run_controller(Path(temp))

        dashboard_class.return_value.cleanup.assert_called_once()

    @patch("scripts.task_graph_cli.TerminalDashboard")
    @patch("scripts.task_graph_cli.TaskGraphController")
    @patch("scripts.task_graph_cli.time.sleep")
    def test_controller_lifecycle_uses_dashboard_and_leaves_final_summary(self, sleep, controller_class, dashboard_class):
        with tempfile.TemporaryDirectory() as temp:
            controller = controller_class.return_value
            controller.is_complete.side_effect = [False, True, True]
            controller.state = {"tasks": {"001": {"status": "integrated"}}}
            controller.tasks = {"001": {"instructions": "Finish."}}
            task_graph_cli.run_controller(Path(temp))

            dashboard_class.return_value.start.assert_called_once()
            controller.run_once.assert_called_once()
            dashboard_class.return_value.finish.assert_called_once()
            sleep.assert_not_called()
    def test_start_accepts_fixed_max_worker_limit(self):
        args = build_parser().parse_args(["start", "demo-plan", "--max-workers", "3"])

        self.assertEqual("start", args.action)
        self.assertEqual("demo-plan", args.plan_slug)
        self.assertEqual(3, args.max_workers)

    def test_start_accepts_a_persisted_worker_command(self):
        args = build_parser().parse_args(
            ["start", "demo-plan", "--worker-command", "/tmp/controller-eval-worker"]
        )

        self.assertEqual("/tmp/controller-eval-worker", args.worker_command)

    def test_eval_controller_is_not_a_runtime_cli_command(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            build_parser().parse_args(["eval-controller"])

        self.assertEqual(2, error.exception.code)

    def test_resume_remains_a_runtime_cli_command(self):
        args = build_parser().parse_args(["resume", "demo-plan", "run-1"])

        self.assertEqual("resume", args.action)
        self.assertEqual("demo-plan", args.plan_slug)
        self.assertEqual("run-1", args.run_id)

    def test_status_and_merge_are_runtime_cli_commands(self):
        status_args = build_parser().parse_args(["status", "demo-plan"])
        merge_args = build_parser().parse_args(["merge", "demo-plan", "--run-id", "run-1"])

        self.assertEqual("status", status_args.action)
        self.assertIsNone(status_args.run_id)
        self.assertEqual("merge", merge_args.action)
        self.assertEqual("run-1", merge_args.run_id)

    def test_controller_command_uses_the_immutable_run_directory(self):
        command = controller_command(Path("/repo/.agent/demo/runs/run-1"))

        self.assertIn("controller", command)
        self.assertIn("--run-dir", command)
        self.assertIn("/repo/.agent/demo/runs/run-1", command)

    def test_cli_runs_directly_as_a_script(self):
        result = subprocess.run(
            [sys.executable, "scripts/task_graph_cli.py", "--help"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("start", result.stdout)
        self.assertIn("resume", result.stdout)
        self.assertNotIn("eval-controller", result.stdout)

    @patch("scripts.task_graph_cli.ensure_clean_base")
    @patch("scripts.task_graph_cli.TaskGraphGit")
    @patch("scripts.task_graph_cli._repository_root")
    def test_start_rejects_unresolvable_git_common_dir_before_creating_resources(
        self, repository_root, git_class, ensure_clean
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".agent" / "demo-plan").mkdir(parents=True)
            repository_root.return_value = root
            git = git_class.return_value
            git.common_dir.side_effect = TaskGraphGitError("not a git repository")

            with self.assertRaisesRegex(TaskGraphRuntimeError, "cannot resolve shared Git metadata directory"):
                task_graph_cli.start("demo-plan", 1)

            git.create_branch.assert_not_called()
            git.add_worktree.assert_not_called()
            git.head_sha.assert_not_called()

    @patch("scripts.task_graph_cli._repository_root")
    def test_resume_rejects_legacy_state_without_git_common_dir(self, repository_root):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / ".agent" / "demo-plan" / "runs" / "run-1"
            state = create_state(
                run_id="run-1",
                plan_slug="demo-plan",
                repository=str(root),
                feature_branch="task-graph/demo-plan/run-1",
                base_commit="abc123",
                snapshot_digest="digest",
                task_digests={"001-first": "task-digest"},
                max_workers=1,
                task_ids=["001-first"],
                git_common_dir="/repo/.git",
            )
            del state["gitCommonDir"]
            write_state(run_dir, state)
            repository_root.return_value = root

            with self.assertRaisesRegex(TaskGraphRuntimeError, "start a fresh run from a clean base"):
                task_graph_cli.resume("demo-plan", "run-1")
