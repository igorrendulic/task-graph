import subprocess
import unittest
from pathlib import Path

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
