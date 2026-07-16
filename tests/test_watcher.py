import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WATCHER_PATH = ROOT / "scripts" / "watcher.py"
SPEC = importlib.util.spec_from_file_location("watcher", WATCHER_PATH)
assert SPEC and SPEC.loader
WATCHER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WATCHER
SPEC.loader.exec_module(WATCHER)


class WatcherTest(unittest.TestCase):
    def test_watcher_checkpoint_cli_returns_when_no_workers_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".agent").mkdir()
            result = __import__("subprocess").run(
                [
                    sys.executable,
                    str(WATCHER_PATH),
                    "watch-exec",
                    "--checkpoint",
                    "--repo",
                    str(repo),
                    "--seconds",
                    "5",
                ],
                text=True,
                capture_output=True,
            )

        self.assertEqual(0, result.returncode)
        self.assertIn("checkpoint: no active exec workers", result.stdout)

    def test_checkpoint_signals_actionable_status_without_sleeping(self) -> None:
        entries = [{"state": "NEEDS_ATTENTION", "task": "001-work.md"}]
        with patch.object(WATCHER.KANBAN, "collect_status", return_value=entries), patch.object(
            WATCHER.KANBAN, "print_status"
        ) as print_status, patch("sys.stdout", new_callable=io.StringIO) as output:
            result = WATCHER.watch_exec(Path("/repo"), "plan", "run", "001-work.md", 5, checkpoint=True)

        self.assertEqual(0, result)
        self.assertIn("signal: NEEDS_ATTENTION", output.getvalue())
        print_status.assert_called_once_with(entries)

    def test_legacy_kanban_watch_command_forwards_to_watcher(self) -> None:
        kanban_path = ROOT / "scripts" / "kanban.py"
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".agent").mkdir()
            result = __import__("subprocess").run(
                [
                    sys.executable,
                    str(kanban_path),
                    "watch-exec",
                    "--checkpoint",
                    "--repo",
                    str(repo),
                    "--seconds",
                    "5",
                ],
                text=True,
                capture_output=True,
            )

        self.assertEqual(0, result.returncode)
        self.assertIn("checkpoint: no active exec workers", result.stdout)


if __name__ == "__main__":
    unittest.main()
