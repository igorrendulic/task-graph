import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_activity import WorkerActivity


class WorkerActivityTests(unittest.TestCase):
    def test_reads_only_complete_new_events_and_keeps_activity_age(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.stdout'
            reader = WorkerActivity()
            event = json.dumps({'type': 'item.started', 'item': {
                'id': 'one', 'type': 'command_execution', 'command': 'npm test'}})
            path.write_text(event[:20])
            self.assertEqual([], reader.read(path))
            with path.open('a') as stream:
                stream.write(event[20:] + '\n')
            os.utime(path, (100, 100))
            self.assertEqual(['Running: npm test'], reader.read(path))
            self.assertEqual(100, reader.since)
            self.assertEqual([], reader.read(path))
            with path.open('a') as stream:
                stream.write(event.replace('started', 'updated') + '\n')
            os.utime(path, (110, 110))
            self.assertEqual([], reader.read(path))
            self.assertEqual(100, reader.since)
            self.assertEqual(110, reader.last_event_at)

    def test_replacement_and_resume_logs_reset_activity_without_affecting_task_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.stdout'
            reader = WorkerActivity()
            path.write_text('{"type":"turn.completed"}\n')
            self.assertEqual(['Agent turn completed'], reader.read(path))
            replacement = path.with_suffix('.new')
            replacement.write_text('{"type":"turn.started"}\n')
            replacement.replace(path)
            self.assertEqual(['Agent turn started'], reader.read(path))
            missing = path.with_suffix('.resume')
            self.assertEqual([], reader.read(missing))
            self.assertEqual('', reader.text)

    def test_ignores_malformed_events_and_strips_terminal_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.stdout'
            path.write_text('broken\n[]\n{"type": []}\n{"type": "item.started", "item": {"type": {}}}\n' + json.dumps({'type': 'item.completed', 'item': {
                'type': 'agent_message', 'text': 'Done\x1b[2J\nNext'}}) + '\n')
            reader = WorkerActivity()
            self.assertEqual(['Done Next'], reader.read(path))

    def test_large_unterminated_line_does_not_grow_memory_without_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.stdout'
            path.write_bytes(b'x' * 600_000 + b'\n{"type":"turn.started"}\n')
            reader = WorkerActivity()
            events = []
            for _ in range(8):
                events.extend(reader.read(path))
            self.assertEqual(['Agent turn started'], events)
