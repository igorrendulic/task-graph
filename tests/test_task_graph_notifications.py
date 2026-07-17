import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.task_graph_notifications import notify_completion


class TaskGraphNotificationTests(unittest.TestCase):
    @patch("scripts.task_graph_notifications.subprocess.run")
    @patch("scripts.task_graph_notifications.platform.system", return_value="Darwin")
    def test_macos_notification_contains_the_completion_payload(self, system, run):
        run.return_value = CompletedProcess([], 0, stderr="")

        outcome = notify_completion(succeeded=True, message="merge demo")

        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(["osascript", "-e"], command[:2])
        self.assertIn("merge demo", command[2])
        self.assertIn("Task Graph succeeded", command[2])
        self.assertEqual({"outcome": "delivered"}, outcome)

    @patch("scripts.task_graph_notifications.subprocess.run")
    @patch("scripts.task_graph_notifications.platform.system", return_value="Linux")
    def test_unsupported_platform_does_not_invoke_a_notification_command(self, system, run):
        outcome = notify_completion(succeeded=False, message="status demo")

        run.assert_not_called()
        self.assertEqual({"outcome": "unsupported"}, outcome)

    @patch("scripts.task_graph_notifications.subprocess.run", side_effect=OSError("missing osascript"))
    @patch("scripts.task_graph_notifications.platform.system", return_value="Darwin")
    def test_notification_failure_is_ignored(self, system, run):
        outcome = notify_completion(succeeded=False, message="status demo")

        self.assertEqual(
            {"outcome": "failed", "error": "osascript unavailable: missing osascript"}, outcome
        )

    @patch("scripts.task_graph_notifications.subprocess.run")
    @patch("scripts.task_graph_notifications.platform.system", return_value="Darwin")
    def test_nonzero_osascript_exit_records_a_sanitized_error(self, system, run):
        run.return_value = CompletedProcess([], 3, stderr="denied\n\x1b[31mby macOS\x1b[0m\n")

        outcome = notify_completion(succeeded=False, message="status demo")

        self.assertEqual(
            {"outcome": "failed", "error": "osascript exited 3: denied by macOS"},
            outcome,
        )
