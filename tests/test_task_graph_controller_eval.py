import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.task_graph_controller_eval import (
    _controller_is_live,
    _scenario_diagnostics,
    _write_worker,
    run_controller_evals,
)


class TaskGraphControllerEvalTests(unittest.TestCase):
    @patch("scripts.task_graph_controller_eval._run_scenario")
    @patch("scripts.task_graph_controller_eval._require_executables")
    @patch("scripts.task_graph_controller_eval.time.monotonic")
    def test_reports_each_scenario_and_a_passing_summary(self, monotonic, require_executables, run_scenario):
        monotonic.side_effect = [10.0, 10.25, 11.0, 11.5, 12.0, 12.75, 13.0, 14.0, 15.0, 16.25]
        output = StringIO()

        with patch("sys.stdout", output):
            run_controller_evals()

        self.assertEqual(5, run_scenario.call_count)
        self.assertIn(
            "RUN  parallel-success — verifies two independent tasks execute concurrently and integrate\n",
            output.getvalue(),
        )
        self.assertIn("PASS parallel-success (0.2s)\n", output.getvalue())
        self.assertIn("PASS resume (1.2s)\n", output.getvalue())
        self.assertIn("Summary: 5 passed, 0 failed\n", output.getvalue())
        require_executables.assert_called_once_with()

    @patch("scripts.task_graph_controller_eval._run_scenario", side_effect=RuntimeError("boom"))
    @patch("scripts.task_graph_controller_eval._require_executables")
    @patch("scripts.task_graph_controller_eval.time.monotonic", side_effect=[10.0, 10.5])
    def test_reports_a_failed_scenario_without_a_successful_summary(
        self, monotonic, require_executables, run_scenario
    ):
        output = StringIO()

        with patch("sys.stdout", output):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                run_controller_evals()

        self.assertIn(
            "RUN  parallel-success — verifies two independent tasks execute concurrently and integrate\n",
            output.getvalue(),
        )
        self.assertIn("FAIL parallel-success (0.5s)\n", output.getvalue())
        self.assertNotIn("Summary:", output.getvalue())
        require_executables.assert_called_once_with()
        run_scenario.assert_called_once()

    def test_scripted_worker_uses_the_absolute_active_python_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = _write_worker(Path(temp))

            self.assertTrue(worker.read_text(encoding="utf-8").startswith(f"#!{sys.executable}\n"))

    @patch("scripts.task_graph_controller_eval.TmuxClient.pane_is_live", return_value=False)
    def test_controller_liveness_uses_the_saved_pane_pid_pair(self, pane_is_live):
        self.assertFalse(_controller_is_live({"paneId": "%10", "pid": 1234}))

        pane_is_live.assert_called_once_with("%10", 1234)

    @patch("scripts.task_graph_controller_eval._tmux")
    def test_failure_diagnostics_include_scenario_tmux_and_controller_state(self, tmux):
        tmux.side_effect = [
            subprocess.CompletedProcess(["tmux"], 0, "", ""),
            subprocess.CompletedProcess(["tmux"], 1, "", "no such session"),
        ]

        class Run:
            def state(self):
                return {"controller": {"paneId": "%10", "pid": 1234}}

        diagnostics = _scenario_diagnostics("resume", Run(), "task-graph-eval-run")

        self.assertIn("scenario=resume", diagnostics)
        self.assertIn("tmux has-session: returncode=0", diagnostics)
        self.assertIn("tmux list-windows: returncode=1, stderr='no such session'", diagnostics)
        self.assertIn('controller={"paneId": "%10", "pid": 1234}', diagnostics)
