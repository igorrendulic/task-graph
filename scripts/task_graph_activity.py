"""Bounded, incremental observation of worker logs; never schedules work."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def terminal_text(value: str) -> str:
    """Keep agent-controlled text from moving the terminal cursor."""
    value = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', value)
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ' '.join(''.join(c for c in value if c.isprintable() or c.isspace()).split())


def _activity(event: dict[str, Any]) -> str:
    kind = event.get('type')
    if not isinstance(kind, str):
        return ''
    if kind in {'thread.started', 'turn.started', 'turn.completed'}:
        return {'thread.started': 'Worker started', 'turn.started': 'Agent turn started',
                'turn.completed': 'Agent turn completed'}[kind]
    if kind in {'error', 'turn.failed'}:
        error = event.get('error', event.get('message', 'Agent turn failed'))
        return 'Error: ' + str(error.get('message', error) if isinstance(error, dict) else error)
    item = event.get('item')
    if not isinstance(item, dict):
        return ''
    item_kind = item.get('type')
    if not isinstance(item_kind, str):
        return ''
    if item_kind == 'command_execution':
        command = str(item.get('command') or item.get('cmd') or '<unknown command>')
        if kind == 'item.completed':
            return f"Command exit {item.get('exit_code', '?')}: {command}"
        return f'Running: {command}'
    if item_kind == 'file_change':
        changes = item.get('changes')
        if isinstance(changes, list):
            paths = ', '.join(str(c.get('path', '?')) for c in changes if isinstance(c, dict))
            return ('Changed: ' if kind == 'item.completed' else 'Editing: ') + paths
    if item_kind in {'agent_message', 'message'}:
        return str(item.get('text') or item.get('content') or '')
    if item_kind == 'error':
        return 'Error: ' + str(item.get('message') or item.get('error') or 'Worker error')
    return ''


class WorkerActivity:
    """One cursor per task invocation, including partial lines and log replacement."""

    READ_LIMIT = 256 * 1024

    def __init__(self) -> None:
        self.path: Path | None = None
        self.identity: tuple[int, int] | None = None
        self.offset = 0
        self.pending = b''
        self.discard_line = False
        self.text = ''
        self.since: float | None = None
        self.last_event_at: float | None = None
        self.thread_id: str | None = None

    def read(self, path: Path) -> list[str]:
        if path != self.path:
            self.__init__()
            self.path = path
        try:
            with path.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.identity != identity or stat.st_size < self.offset:
                    self.__init__()
                    self.path, self.identity = path, identity
                stream.seek(self.offset)
                data = stream.read(self.READ_LIMIT)
                self.offset = stream.tell()
        except OSError:
            return []
        lines = (self.pending + data).split(b'\n')
        self.pending = lines.pop()
        updates = []
        for line in lines:
            if self.discard_line:
                self.discard_line = False
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                self.thread_id = event['thread_id']
            # File time avoids presenting replayed history as fresh activity.
            self.last_event_at = stat.st_mtime
            text = terminal_text(_activity(event))[:2000]
            if text and text != self.text:
                self.text, self.since = text, stat.st_mtime
                updates.append(text)
        if len(self.pending) > self.READ_LIMIT:
            self.pending = b''
            self.discard_line = True
        return updates
