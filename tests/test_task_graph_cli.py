import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts import task_graph_cli
from scripts.task_graph_cli import build_parser, controller_command
from scripts.task_graph_git import TaskGraphGitError
from scripts.task_graph_runtime import TaskGraphRuntimeError, create_state, write_state


class TaskGraphCliTests(unittest.TestCase):
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
