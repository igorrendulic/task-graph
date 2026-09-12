import io
import unittest
from unittest.mock import patch

from scripts.task_graph_display import TerminalDashboard, format_dashboard
from scripts.task_graph_preview import preview_frame, run_preview


class TaskGraphPreviewTests(unittest.TestCase):
    def test_frames_show_all_phases_and_repeat_with_fresh_timestamps(self):
        state, tasks, activity = preview_frame(100.0, 100.0)
        panel = format_dashboard(state, tasks, activity=activity, now=100.0, width=140)
        for phase in ('working', 'verifying', 'awaiting integration', 'integrating',
                      'waiting', 'retrying', 'failed', 'blocked', 'integrated'):
            self.assertIn(phase, panel)
        self.assertIn('DEMO', panel)
        self.assertIn('Ctrl+C', panel)

        expected = ('working', 'working', 'verifying', 'awaiting integration',
                    'integrating', 'integrated', 'working')
        for step, phase in enumerate(expected):
            now = 100.0 + step * 5
            state, tasks, activity = preview_frame(100.0, now)
            panel = format_dashboard(state, tasks, activity=activity, now=now, width=140)
            row = next(line for line in panel.splitlines() if '001-dashboard' in line)
            self.assertIn(phase, row)
        self.assertEqual(130.0, activity['001-dashboard'].since)
        self.assertEqual(100.0, state['createdAt'])

    def test_activity_changes_without_mutating_previous_frame(self):
        first = preview_frame(100.0, 100.0)
        second = preview_frame(100.0, 105.0)
        self.assertNotEqual(first[2]['001-dashboard'].text, second[2]['001-dashboard'].text)
        self.assertEqual(100.0, first[2]['001-dashboard'].since)

    def test_interrupt_restores_terminal_and_records_simulated_events(self):
        output = io.StringIO()
        dashboard = TerminalDashboard(output, size_provider=lambda: (140, 30))
        with patch('scripts.task_graph_preview.time.time', side_effect=[100.0, 105.0]), \
                patch('scripts.task_graph_preview.time.sleep', side_effect=[None, KeyboardInterrupt]):
            run_preview(dashboard)
        rendered = output.getvalue()
        self.assertIn('SIMULATED', rendered)
        self.assertIn('Editing:', rendered)
        self.assertTrue(rendered.endswith('\x1b[r\x1b[?25h'))

    def test_render_failure_also_restores_terminal(self):
        output = io.StringIO()
        dashboard = TerminalDashboard(output, size_provider=lambda: (_ for _ in ()).throw(RuntimeError('size')))
        with self.assertRaisesRegex(RuntimeError, 'size'):
            run_preview(dashboard)
        self.assertTrue(output.getvalue().endswith('\x1b[r\x1b[?25h'))
