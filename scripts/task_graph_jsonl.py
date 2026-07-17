"""Render Codex JSONL progress events as readable terminal output."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO


def format_jsonl_line(line: str) -> str:
    """Return one compact display line for a Codex JSONL event."""
    raw = line.strip()
    if not raw:
        return ""
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return f"[raw] {raw}"
    if not isinstance(event, dict):
        return f"[event] {json.dumps(event, separators=(',', ':'))}"

    event_type = event.get("type")
    if event_type == "thread.started":
        thread_id = event.get("thread_id")
        return f"[task] started {thread_id}" if thread_id else "[task] started"
    if event_type == "turn.started":
        return "[task] started"
    if event_type == "turn.completed":
        return "[task] completed"
    if event_type == "error":
        return f"[error] {_text(event.get('message') or event.get('error') or event)}"

    item = event.get("item")
    if isinstance(item, dict):
        return _format_item(item, event)
    return f"[event] {json.dumps(event, separators=(',', ':'))}"


def format_stream(source: TextIO, output: TextIO) -> None:
    """Format source line-by-line and flush each rendered event immediately."""
    for line in source:
        rendered = format_jsonl_line(line)
        if rendered:
            print(rendered, file=output, flush=True)


def _format_item(item: dict[str, Any], event: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == "command_execution":
        command = _text(item.get("command") or item.get("cmd") or "<unknown command>")
        exit_code = item.get("exit_code")
        suffix = f" (exit {exit_code})" if exit_code is not None else ""
        return f"[command] $ {command}{suffix}"
    if item_type == "file_change":
        changes = item.get("changes")
        if isinstance(changes, list):
            rendered = [
                f"{change.get('kind', 'changed')} {change.get('path', '<unknown path>')}"
                for change in changes
                if isinstance(change, dict)
            ]
            if rendered:
                return "[files] " + "; ".join(rendered)
        return "[files] changed"
    if item_type in {"agent_message", "message"}:
        return f"[agent] {_text(item.get('text') or item.get('content') or '')}"
    if item_type == "error":
        return f"[error] {_text(item.get('message') or item.get('error') or item)}"
    return f"[event] {json.dumps(event, separators=(',', ':'))}"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return json.dumps(value, separators=(",", ":"))


if __name__ == "__main__":
    format_stream(sys.stdin, sys.stdout)
