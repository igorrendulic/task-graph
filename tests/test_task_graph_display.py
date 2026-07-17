import io
import unittest

from scripts.task_graph_display import TerminalDashboard, format_dashboard


def _state():
    return {
        "runId": "run-1", "planSlug": "demo", "createdAt": 90.0,
        "tasks": {
            "001-first": {"status": "integrated", "attempts": [{}]},
            "002-second": {"status": "running", "attempts": [{"startedAt": 95.0}]},
            "003-third": {"status": "pending", "attempts": []},
            "004-fourth": {"status": "retrying", "attempts": [{"failureSummary": "worker exit 1; details"}]},
            "005-fifth": {"status": "failed", "attempts": [{"failureSummary": "worker exit 1"}, {"failureSummary": "cherry-pick failed: conflict"}]},
            "006-sixth": {"status": "blocked", "attempts": [], "blockedBy": "005-fifth"},
        },
    }


TASKS = {
    "001-first": {"instructions": "Finish the foundation."},
    "002-second": {"instructions": "Build the live dashboard interface."},
    "003-third": {"instructions": "Write the documentation."},
    "004-fourth": {"instructions": "Retry the worker."},
    "005-fifth": {"instructions": "Resolve the conflict."},
    "006-sixth": {"instructions": "Ship the dependent task."},
}


class TaskGraphDisplayTests(unittest.TestCase):
    def test_formatter_shows_run_progress_and_task_specific_details(self):
        panel = format_dashboard(_state(), TASKS, now=105.0, width=120)

        self.assertIn("demo / run-1", panel)
        self.assertIn("1/6 complete", panel)
        self.assertIn("1 running", panel)
        self.assertIn("16%", panel)
        self.assertIn("elapsed 00:15", panel)
        self.assertIn("✓ 001-first", panel)
        self.assertIn("● 002-second", panel)
        self.assertIn("running 00:10", panel)
        self.assertIn("○ 003-third", panel)
        self.assertIn("waiting for 002-second", panel)
        self.assertIn("↻ 004-fourth", panel)
        self.assertIn("attempt 1: worker exit 1", panel)
        self.assertIn("✗ 005-fifth", panel)
        self.assertIn("attempt 2: cherry-pick failed: conflict", panel)
        self.assertIn("⊘ 006-sixth", panel)
        self.assertIn("blocked by 005-fifth", panel)
        self.assertIn("\x1b[", panel)

    def test_formatter_uses_compact_two_line_rows_under_eighty_columns(self):
        panel = format_dashboard(_state(), TASKS, now=105.0, width=60)
        lines = panel.splitlines()
        running_index = next(index for index, line in enumerate(lines) if "002-second" in line)

        self.assertIn("Build the live dashboard", lines[running_index])
        self.assertIn("running 00:10", lines[running_index + 1])

    def test_terminal_adapter_reserves_panel_redraws_for_resize_and_cleans_up(self):
        output = io.StringIO()
        size = [(100, 30), (60, 20)]
        dashboard = TerminalDashboard(output, size_provider=lambda: size.pop(0))

        dashboard.start(_state(), TASKS, now=105.0)
        dashboard.redraw(_state(), TASKS, now=106.0)
        dashboard.finish(_state(), TASKS, "run complete: 1 integrated, 2 failed/blocked", now=107.0)

        sequence = output.getvalue()
        self.assertIn("\x1b[?25l", sequence)
        self.assertIn("\x1b[", sequence)
        self.assertIn(";30r", sequence)
        self.assertIn(";20r", sequence)
        self.assertIn("run complete: 1 integrated, 2 failed/blocked", sequence)
        self.assertIn("\x1b[r", sequence)
        self.assertTrue(sequence.endswith("\x1b[?25h"))
