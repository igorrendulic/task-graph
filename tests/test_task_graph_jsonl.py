import io
import unittest

from scripts.task_graph_jsonl import format_jsonl_line, format_stream


class TaskGraphJsonlTests(unittest.TestCase):
    def test_formats_representative_codex_events(self):
        cases = [
            ('{"type":"thread.started","thread_id":"thread-123"}', "[task] started thread-123"),
            ('{"type":"turn.completed"}', "[task] completed"),
            (
                '{"type":"item.completed","item":{"type":"command_execution","command":"git status","exit_code":0}}',
                "[command] $ git status (exit 0)",
            ),
            (
                '{"type":"item.completed","item":{"type":"file_change","changes":[{"path":"scripts/controller.py","kind":"modified"},{"path":"tests/test_controller.py","kind":"added"}]}}',
                "[files] modified scripts/controller.py; added tests/test_controller.py",
            ),
            (
                '{"type":"item.completed","item":{"type":"agent_message","text":"Focused tests pass."}}',
                "[agent] Focused tests pass.",
            ),
            ('{"type":"error","message":"rate limited"}', "[error] rate limited"),
        ]

        self.assertEqual([expected for _, expected in cases], [format_jsonl_line(line) for line, _ in cases])

    def test_compacts_unknown_events_and_preserves_malformed_lines(self):
        self.assertEqual('[event] {"type":"future.event","detail":"kept"}', format_jsonl_line('{"type":"future.event","detail":"kept"}'))
        self.assertEqual("[raw] not json", format_jsonl_line("not json"))

    def test_formats_stream_and_flushes_each_line(self):
        output = io.StringIO()

        format_stream(
            io.StringIO('{"type":"turn.started"}\n{"type":"turn.completed"}\n'),
            output,
        )

        self.assertEqual("[task] started\n[task] completed\n", output.getvalue())

