import json
import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_controller import TaskGraphController
from scripts.task_graph_runtime import create_run_snapshot, create_state, load_state, write_state
from scripts.task_graph_tmux import PaneInfo


class FakeGit:
    def __init__(self) -> None:
        self.worker_calls: list[tuple[Path, str, str]] = []

    def head_sha(self, worktree: Path) -> str:
        return "feature-head"

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
            self.assertIn("--json", tmux.commands[0])

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
