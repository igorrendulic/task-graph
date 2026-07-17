import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.task_graph_tmux import PaneInfo, TmuxClient


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "%10\n", "")
        if "new-window" in command:
            return subprocess.CompletedProcess(command, 0, "%11\n", "")
        if "display-message" in command:
            return subprocess.CompletedProcess(command, 0, "%10 1234\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class TmuxClientTests(unittest.TestCase):
    def test_creates_controller_session_and_returns_pane_identity(self):
        runner = FakeRunner()
        tmux = TmuxClient(runner=runner)

        pane_id = tmux.create_session("task-graph-demo-run-1", Path("/repo"), "controller command")

        self.assertEqual("%10", pane_id)
        command = runner.calls[0]
        self.assertEqual("new-session", command[1])
        self.assertIn("task-graph-demo-run-1", command)
        self.assertIn("controller", command)
        self.assertIn("controller command", command)

    def test_starts_worker_window_and_reads_pane_pid(self):
        runner = FakeRunner()
        tmux = TmuxClient(runner=runner)

        pane_id = tmux.create_window("task-graph-demo-run-1", "001-first", Path("/worktree"), "worker command")
        info = tmux.pane_info(pane_id)

        self.assertEqual("%11", pane_id)
        self.assertEqual(PaneInfo(pane_id="%10", pid=1234), info)
        self.assertIn("tmux set-option -p remain-on-exit on && exec worker command", runner.calls[0])

    def test_pane_is_not_live_when_tmux_no_longer_lists_it(self):
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "no such pane")

        self.assertFalse(TmuxClient(runner=runner).pane_is_live("%10", 1234))

    @patch("scripts.task_graph_tmux.os.kill", side_effect=ProcessLookupError)
    def test_pane_is_not_live_when_tmux_retains_a_dead_pane(self, kill):
        self.assertFalse(TmuxClient(runner=FakeRunner()).pane_is_live("%10", 1234))

        kill.assert_called_once_with(1234, 0)

    @patch("scripts.task_graph_tmux.os.kill")
    def test_pane_is_live_when_tmux_pid_and_process_match(self, kill):
        self.assertTrue(TmuxClient(runner=FakeRunner()).pane_is_live("%10", 1234))

        kill.assert_called_once_with(1234, 0)
