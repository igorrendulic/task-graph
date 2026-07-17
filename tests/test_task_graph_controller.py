import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_controller import TaskGraphController
from scripts.task_graph_runtime import (
    TaskGraphRuntimeError,
    create_run_snapshot,
    create_state,
    load_state,
    write_state,
)
from scripts.task_graph_tmux import PaneInfo


class FakeGit:
    def __init__(self) -> None:
        self.worker_calls: list[tuple[Path, str, str]] = []

    def head_sha(self, worktree: Path) -> str:
        return "feature-head"

    def common_dir(self) -> Path:
        return Path("/repo/.git")

    def create_worker_worktree(self, path: Path, branch: str, base: str) -> None:
        path.mkdir(parents=True)
        self.worker_calls.append((path, branch, base))

    def inspect_one_task_commit(self, worktree: Path, base: str):
        raise AssertionError("not used by this test")

    def cherry_pick(self, integration: Path, commit: str) -> None:
        raise AssertionError("not used by this test")

    def abort_cherry_pick(self, integration: Path) -> bool:
        return False

    def is_ancestor(self, commit: str, worktree: Path) -> bool:
        return False

    def remove_worktree(self, path: Path) -> None:
        pass


class FakeTmux:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def create_window(self, session: str, name: str, cwd: Path, command: str) -> str:
        self.commands.append(command)
        return "%20"

    def pane_info(self, pane_id: str) -> PaneInfo:
        return PaneInfo(pane_id=pane_id, pid=1234)

    def pane_is_live(self, pane_id: str, pid: int) -> bool:
        return False


def _make_plan(root: Path) -> Path:
    plan = root / ".agent" / "demo"
    todo = plan / "todo"
    todo.mkdir(parents=True)
    (todo / "001-first.md").write_text("# First\n\n## Dependencies\n\nNone\n")
    (todo / "002-second.md").write_text("# Second\n\n## Dependencies\n\n- 001-first.md\n")
    (plan / "dag.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "planSlug": "demo",
                "tasks": [
                    {
                        "id": "001-first", "taskFile": "001-first.md", "title": "First",
                        "instructions": "First.", "predictedPaths": [], "predictedSymbols": [],
                        "dependsOn": [], "parallelSafe": True, "schedulingRationale": "isolated",
                    },
                    {
                        "id": "002-second", "taskFile": "002-second.md", "title": "Second",
                        "instructions": "Second.", "predictedPaths": [], "predictedSymbols": [],
                        "dependsOn": ["001-first"], "parallelSafe": False, "schedulingRationale": "depends",
                    },
                ],
            }
        )
    )
    return plan


class TaskGraphControllerTests(unittest.TestCase):
    def test_transition_events_are_emitted_once_for_real_lifecycle_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = _make_plan(root)
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run)
            state = create_state(
                run_id="run-1", plan_slug="demo", repository=str(root),
                feature_branch="task-graph/demo/run-1/feature", base_commit="base",
                snapshot_digest=snapshot.dag_digest, task_digests=snapshot.task_digests,
                max_workers=1, task_ids=["001-first", "002-second"],
                git_common_dir="/repo/.git",
            )
            state["integrationWorktree"] = str(run / "integration")
            write_state(run, state)
            events = []
            controller = TaskGraphController(run, git=FakeGit(), tmux=FakeTmux(), event_sink=events.append)

            controller._transition("001-first", "running", "launch")
            controller._transition("001-first", "running", "launch")
            controller._transition("001-first", "retrying", "retry", "worker exit 1")

            self.assertEqual(
                [
                    {"kind": "launch", "taskId": "001-first", "from": "pending", "to": "running"},
                    {"kind": "retry", "taskId": "001-first", "from": "running", "to": "retrying", "detail": "worker exit 1"},
                ], events,
            )
    def test_worker_command_streams_formatted_output_and_preserves_raw_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = _make_plan(root)
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run)
            state = create_state(
                run_id="run-1", plan_slug="demo", repository=str(root),
                feature_branch="task-graph/demo/run-1/feature", base_commit="base",
                snapshot_digest=snapshot.dag_digest, task_digests=snapshot.task_digests,
                max_workers=1, task_ids=["001-first", "002-second"],
                git_common_dir="/repo/.git",
            )
            state["integrationWorktree"] = str(run / "integration")
            write_state(run, state)
            worker = root / "worker.py"
            worker.write_text(
                f"#!{sys.executable}\n"
                "import json\n"
                "import sys\n"
                "print(json.dumps({'type': 'turn.started'}), flush=True)\n"
                "print('worker diagnostic', file=sys.stderr, flush=True)\n"
                "print(json.dumps({'type': 'turn.completed'}), flush=True)\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            worker.chmod(worker.stat().st_mode | 0o111)
            logs = run / "logs"
            logs.mkdir(parents=True)
            attempt = {
                "stdoutLog": str(logs / "worker.stdout"),
                "stderrLog": str(logs / "worker.stderr"),
                "combinedLog": str(logs / "worker.log"),
                "exitFile": str(logs / "worker.exit"),
            }
            controller = TaskGraphController(run, git=FakeGit(), tmux=FakeTmux(), codex_bin=str(worker))

            result = subprocess.run(
                controller._worker_command(root, "001-first", attempt, None),
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(7, result.returncode, result.stderr)
            self.assertEqual("[task] started\n[task] completed\n", result.stdout)
            self.assertEqual("worker diagnostic\n", result.stderr)
            self.assertIn('"type": "turn.started"', Path(attempt["stdoutLog"]).read_text(encoding="utf-8"))
            self.assertEqual("worker diagnostic\n", Path(attempt["stderrLog"]).read_text(encoding="utf-8"))
            combined = Path(attempt["combinedLog"]).read_text(encoding="utf-8")
            self.assertIn('"type": "turn.started"', combined)
            self.assertIn("worker diagnostic", combined)
            self.assertIn('"type": "turn.completed"', combined)
            self.assertEqual("7\n", Path(attempt["exitFile"]).read_text(encoding="utf-8"))

    def test_worker_prompt_embeds_snapshot_dependency_sha_and_repair_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = _make_plan(root)
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run)
            state = create_state(
                run_id="run-1", plan_slug="demo", repository=str(root),
                feature_branch="task-graph/demo/run-1/feature", base_commit="base",
                snapshot_digest=snapshot.dag_digest, task_digests=snapshot.task_digests,
                max_workers=2, task_ids=["001-first", "002-second"],
                git_common_dir="/repo/.git",
            )
            state["integrationWorktree"] = str(run / "integration")
            state["planDirectory"] = str(plan)
            state["session"] = "task-graph-demo-run-1"
            state["tasks"]["001-first"].update(status="integrated", commitSha="abc123")
            write_state(run, state)

            controller = TaskGraphController(run, git=FakeGit(), tmux=FakeTmux())
            prompt = controller.build_worker_prompt("002-second", "resolve the conflict")

            self.assertIn("# Second", prompt)
            self.assertIn("001-first: abc123", prompt)
            self.assertIn("resolve the conflict", prompt)

    def test_missing_exit_sentinel_from_a_dead_pane_retries_the_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = _make_plan(root)
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run)
            state = create_state(
                run_id="run-1", plan_slug="demo", repository=str(root),
                feature_branch="task-graph/demo/run-1/feature", base_commit="base",
                snapshot_digest=snapshot.dag_digest, task_digests=snapshot.task_digests,
                max_workers=1, task_ids=["001-first", "002-second"],
                git_common_dir="/repo/.git",
            )
            state["integrationWorktree"] = str(run / "integration")
            state["session"] = "task-graph-demo-run-1"
            state["tasks"]["001-first"].update(
                status="running",
                attempts=[
                    {
                        "worktree": str(run / "worktrees" / "first"),
                        "launchBaseSha": "base",
                        "exitFile": str(run / "logs" / "missing.exit"),
                        "paneId": "%20",
                        "pid": 1234,
                    }
                ],
            )
            write_state(run, state)

            TaskGraphController(run, git=FakeGit(), tmux=FakeTmux()).poll_running_attempts()

            saved = load_state(run)
            self.assertEqual("retrying", saved["tasks"]["001-first"]["status"])
            self.assertIn("without completion", saved["tasks"]["001-first"]["attempts"][0]["failureSummary"])

    def test_ready_dependent_creates_a_fresh_observable_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = _make_plan(root)
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run)
            state = create_state(
                run_id="run-1", plan_slug="demo", repository=str(root),
                feature_branch="task-graph/demo/run-1/feature", base_commit="base",
                snapshot_digest=snapshot.dag_digest, task_digests=snapshot.task_digests,
                max_workers=1, task_ids=["001-first", "002-second"],
                git_common_dir="/repo/.git",
            )
            state["integrationWorktree"] = str(run / "integration")
            state["planDirectory"] = str(plan)
            state["session"] = "task-graph-demo-run-1"
            state["tasks"]["001-first"].update(status="integrated", commitSha="abc123")
            write_state(run, state)
            git = FakeGit()
            tmux = FakeTmux()

            TaskGraphController(run, git=git, tmux=tmux).schedule_ready_tasks()

            saved = load_state(run)
            attempt = saved["tasks"]["002-second"]["attempts"][0]
            self.assertEqual("running", saved["tasks"]["002-second"]["status"])
            self.assertEqual("feature-head", attempt["launchBaseSha"])
            self.assertEqual("%20", attempt["paneId"])
            self.assertEqual(1234, attempt["pid"])
            self.assertTrue(Path(attempt["stdoutLog"]).is_file() is False)
            self.assertIn("worker/002-second/attempt-1", git.worker_calls[0][1])
            self.assertTrue((plan / "in-progress" / "002-second.md").is_file())
            command = tmux.commands[0]
            self.assertIn("--json", command)
            self.assertIn(str(Path(attempt["stdoutLog"])), command)
            self.assertIn(str(Path(attempt["stderrLog"])), command)
            self.assertIn(str(Path(attempt["combinedLog"])), command)
            self.assertIn("task_graph_jsonl.py", command)
            self.assertIn("mkfifo", command)
            self.assertIn("combined_pipe", command)
            self.assertIn("code=$?", command)
            self.assertIn(str(Path(attempt["exitFile"])), command)

    def test_worker_command_comes_from_persisted_run_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = _make_plan(root)
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run)
            state = create_state(
                run_id="run-1", plan_slug="demo", repository=str(root),
                feature_branch="task-graph/demo/run-1/feature", base_commit="base",
                snapshot_digest=snapshot.dag_digest, task_digests=snapshot.task_digests,
                max_workers=1, task_ids=["001-first", "002-second"],
                git_common_dir="/repo/.git",
            )
            state["workerCommand"] = "/tmp/controller-eval-worker"
            state["integrationWorktree"] = str(run / "integration")
            state["planDirectory"] = str(plan)
            state["session"] = "task-graph-demo-run-1"
            state["tasks"]["001-first"].update(status="integrated", commitSha="abc123")
            write_state(run, state)
            tmux = FakeTmux()

            TaskGraphController(run, git=FakeGit(), tmux=tmux).schedule_ready_tasks()

            self.assertIn("/tmp/controller-eval-worker", tmux.commands[0])

    def test_worker_command_allows_shared_git_metadata_and_disables_python_test_caches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = _make_plan(root)
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run)
            state = create_state(
                run_id="run-1", plan_slug="demo", repository=str(root),
                feature_branch="task-graph/demo/run-1/feature", base_commit="base",
                snapshot_digest=snapshot.dag_digest, task_digests=snapshot.task_digests,
                max_workers=1, task_ids=["001-first", "002-second"],
                git_common_dir="/repo/.git",
            )
            state["integrationWorktree"] = str(run / "integration")
            state["planDirectory"] = str(plan)
            state["session"] = "task-graph-demo-run-1"
            state["tasks"]["001-first"].update(status="integrated", commitSha="abc123")
            write_state(run, state)
            tmux = FakeTmux()

            TaskGraphController(run, git=FakeGit(), tmux=tmux).schedule_ready_tasks()

            command = tmux.commands[0]
            self.assertIn("--sandbox workspace-write", command)
            self.assertIn("--add-dir /repo/.git", command)
            self.assertIn("PYTHONDONTWRITEBYTECODE=1", command)
            self.assertIn(
                'PYTEST_ADDOPTS="${PYTEST_ADDOPTS:+${PYTEST_ADDOPTS} }-p no:cacheprovider"',
                command,
            )

    def test_controller_rejects_legacy_state_without_git_common_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = _make_plan(root)
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run)
            state = create_state(
                run_id="run-1", plan_slug="demo", repository=str(root),
                feature_branch="task-graph/demo/run-1/feature", base_commit="base",
                snapshot_digest=snapshot.dag_digest, task_digests=snapshot.task_digests,
                max_workers=1, task_ids=["001-first", "002-second"],
                git_common_dir="/repo/.git",
            )
            del state["gitCommonDir"]
            write_state(run, state)

            with self.assertRaisesRegex(TaskGraphRuntimeError, "start a fresh run from a clean base"):
                TaskGraphController(run, git=FakeGit(), tmux=FakeTmux())
