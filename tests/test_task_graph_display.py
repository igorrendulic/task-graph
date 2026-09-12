import io
import re
import unittest
import os
from unittest.mock import patch

from scripts.task_graph_display import TerminalDashboard, format_dashboard, _terminal_size


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
    "003-third": {"instructions": "Write the documentation.", "dependsOn": ["002-second"]},
    "004-fourth": {"instructions": "Retry the worker."},
    "005-fifth": {"instructions": "Resolve the conflict."},
    "006-sixth": {"instructions": "Ship the dependent task."},
}


class TaskGraphDisplayTests(unittest.TestCase):
    def test_terminal_size_uses_actual_pane_instead_of_stale_environment(self):
        with patch.dict(os.environ, {'COLUMNS': '80', 'LINES': '24'}), \
                patch('scripts.task_graph_display.os.get_terminal_size', return_value=os.terminal_size((140, 30))):
            self.assertEqual((140, 30), _terminal_size())

    def test_formatter_shows_run_progress_and_task_specific_details(self):
        panel = format_dashboard(_state(), TASKS, now=105.0, width=120)

        self.assertIn("demo / run-1", panel)
        self.assertIn("1/6 integrated", panel)
        self.assertIn("1 working", panel)
        self.assertIn("1 waiting", panel)
        self.assertIn("elapsed 00:15", panel)
        self.assertIn("✓ 001-first", panel)
        self.assertIn("integrated", panel)
        self.assertIn("● 002-second", panel)
        self.assertIn("working", panel)
        self.assertIn("00:10", panel)
        self.assertIn("○ 003-third", panel)
        self.assertIn("waiting for 002-second", panel)
        self.assertIn("↻ 004-fourth", panel)
        self.assertIn("attempt 1/2: worker exit 1", panel)
        self.assertIn("✗ 005-fifth", panel)
        self.assertIn("attempt 2/2: cherry-pick failed: conflict", panel)
        self.assertIn("⊘ 006-sixth", panel)
        self.assertIn("blocked by 005-fifth", panel)
        self.assertIn("\x1b[", panel)

    def test_formatter_truncates_instructions_and_does_not_accept_events(self):
        tasks = {**TASKS, "002-second": {"instructions": "Build a dashboard with a deliberately long instruction that cannot fit."}}

        panel = format_dashboard(_state(), tasks, now=105.0, width=40)

        self.assertIn("…", panel)
        with self.assertRaises(TypeError):
            format_dashboard(_state(), tasks, now=105.0, width=40, events=[])

    def test_formatter_uses_compact_two_line_rows_under_eighty_columns(self):
        panel = format_dashboard(_state(), TASKS, now=105.0, width=60)
        lines = panel.splitlines()
        running_index = next(index for index, line in enumerate(lines) if "002-second" in line)

        self.assertIn("Build the live dashboard", lines[running_index + 1])
        self.assertIn("00:10", lines[running_index])

    def test_formatter_bounds_wide_rows_to_the_terminal_width(self):
        panel = format_dashboard(_state(), TASKS, now=105.0, width=80)

        visible_lines = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in panel.splitlines()]

        self.assertTrue(all(len(line) <= 80 for line in visible_lines))

    def test_formatter_keeps_compact_rows_within_a_twenty_column_terminal(self):
        panel = format_dashboard(_state(), TASKS, now=105.0, width=20)

        visible_lines = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in panel.splitlines()]

        self.assertTrue(all(len(line) <= 20 for line in visible_lines))
        self.assertEqual(15, len(visible_lines))

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
        self.assertEqual(2, sequence.count('\x1b[2J'))
        self.assertIn("run complete: 1 integrated, 2 failed/blocked", sequence)
        self.assertIn("\x1b[r", sequence)
        self.assertTrue(sequence.endswith("\x1b[?25h"))

    def test_terminal_adapter_appends_events_below_the_panel(self):
        output = io.StringIO()
        dashboard = TerminalDashboard(output, size_provider=lambda: (100, 30))

        dashboard.start(_state(), TASKS, now=105.0)
        dashboard.record_event({"kind": "launch", "taskId": "002-second"})
        event_output = output.getvalue()
        dashboard.redraw(_state(), TASKS, now=106.0)

        self.assertIn("launch 002-second", event_output)
        self.assertNotIn("launch 002-second", format_dashboard(_state(), TASKS, now=106.0, width=100))

    def test_oversized_panel_is_stable_and_prioritizes_active_and_failed_tasks(self):
        output = io.StringIO()
        dashboard = TerminalDashboard(output, size_provider=lambda: (100, 7))
        first = dashboard._visible_panel(_state(), TASKS, 100, 7, 105.0)
        second = dashboard._visible_panel(_state(), TASKS, 100, 7, 105.0)
        self.assertEqual(first, second)
        text = '\n'.join(first)
        self.assertIn('002-second', text)
        self.assertIn('005-fifth', text)
        self.assertNotIn('001-first', text)
        self.assertIn('hidden', text)
        self.assertLessEqual(len(first), 6)

    def test_recent_events_survive_redraw_and_are_bounded(self):
        output = io.StringIO()
        dashboard = TerminalDashboard(output, size_provider=lambda: (120, 30))
        dashboard.start(_state(), TASKS)
        for index in range(150):
            dashboard.record_event({'kind': 'activity', 'taskId': '002-second', 'detail': f'event-{index}'})
        panel = '\n'.join(dashboard._visible_panel(_state(), TASKS, 120, 30, 105.0))
        self.assertIn('RECENT EVENTS', panel)
        self.assertIn('event-149', panel)
        self.assertNotIn('event-0 ', panel)
        self.assertLessEqual(len(dashboard.recent_events), 100)

    def test_verification_and_integration_are_distinct_from_working(self):
        state = _state()
        state['tasks']['002-second']['attempts'][0].update(
            phase='verifying', phaseStartedAt=100.0,
            verification={'commands': [{'argv': ['npm', 'test'], 'startedAt': 101.0}]},
        )
        tasks = {**TASKS, '002-second': {**TASKS['002-second'], 'verification': {'commands': [['npm', 'test'], ['npm', 'lint']]}}}
        panel = format_dashboard(state, tasks, now=105.0, width=140)
        self.assertIn('1 verifying', panel)
        self.assertIn('Check 1/2: npm test', panel)
        self.assertIn('00:04', panel)
        state['tasks']['002-second']['status'] = 'awaiting_integration'
        self.assertIn('awaiting integration', format_dashboard(state, tasks, width=140))
        state['tasks']['002-second']['status'] = 'integrating'
        self.assertIn('integrating', format_dashboard(state, tasks, width=140))

    def test_shows_current_activity_and_quiet_age_without_claiming_stuck(self):
        from scripts.task_graph_activity import WorkerActivity
        activity = WorkerActivity()
        activity.text, activity.since, activity.last_event_at = 'Running: npm test', 95.0, 100.0
        state = _state()
        state['tasks']['002-second']['attempts'][0]['workerAlive'] = True
        panel = format_dashboard(state, TASKS, now=220.0, width=160, activity={'002-second': activity})
        self.assertIn('Running: npm test', panel)
        self.assertIn('02:05', panel)
        self.assertIn('alive; last event 02:00 ago', panel)
        self.assertNotIn('stuck', panel)

    def test_agent_text_cannot_inject_terminal_controls(self):
        tasks = {**TASKS, '002-second': {'instructions': 'Hello\x1b[2Jworld'}}
        self.assertNotIn('\x1b[2J', format_dashboard(_state(), tasks, width=100))
