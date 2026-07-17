import subprocess
import sys
import unittest
from contextlib import redirect_stderr
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
