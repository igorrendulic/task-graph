import subprocess
import sys
import unittest
from pathlib import Path

from scripts.task_graph_cli import build_parser, controller_command


class TaskGraphCliTests(unittest.TestCase):
    def test_start_accepts_fixed_max_worker_limit(self):
        args = build_parser().parse_args(["start", "demo-plan", "--max-workers", "3"])

        self.assertEqual("start", args.action)
        self.assertEqual("demo-plan", args.plan_slug)
        self.assertEqual(3, args.max_workers)

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
