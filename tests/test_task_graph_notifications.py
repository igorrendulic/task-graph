import unittest
from unittest.mock import patch

from scripts.task_graph_notifications import notify_completion


class TaskGraphNotificationTests(unittest.TestCase):
    @patch("scripts.task_graph_notifications.subprocess.run")
    @patch("scripts.task_graph_notifications.platform.system", return_value="Darwin")
    def test_macos_notification_contains_the_completion_payload(self, system, run):
        notify_completion(succeeded=True, message="merge demo")

        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(["osascript", "-e"], command[:2])
        self.assertIn("merge demo", command[2])
        self.assertIn("Task Graph succeeded", command[2])

    @patch("scripts.task_graph_notifications.subprocess.run")
    @patch("scripts.task_graph_notifications.platform.system", return_value="Linux")
    def test_unsupported_platform_does_not_invoke_a_notification_command(self, system, run):
        notify_completion(succeeded=False, message="status demo")

        run.assert_not_called()

    @patch("scripts.task_graph_notifications.subprocess.run", side_effect=OSError("missing osascript"))
    @patch("scripts.task_graph_notifications.platform.system", return_value="Darwin")
    def test_notification_failure_is_ignored(self, system, run):
        notify_completion(succeeded=False, message="status demo")
