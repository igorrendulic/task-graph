import subprocess
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts import task_graph_cli
from scripts.task_graph_cli import build_parser, controller_command


class TaskGraphCliTests(unittest.TestCase):
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

    def test_controller_eval_is_a_dedicated_cli_command(self):
        args = build_parser().parse_args(["eval-controller"])

        self.assertEqual("eval-controller", args.action)

    @patch("scripts.task_graph_controller_eval.run_controller_evals")
    @patch.object(sys, "argv", ["task_graph_cli.py", "eval-controller"])
    def test_controller_eval_command_runs_the_opt_in_harness(self, run_evals):
        output = StringIO()

        with patch("sys.stdout", output):
            self.assertEqual(0, task_graph_cli.main())

        run_evals.assert_called_once_with()
        self.assertEqual("", output.getvalue())

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
