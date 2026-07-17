"""ANSI rendering for the Task Graph controller's transient terminal dashboard."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO


CSI = "\x1b["
RESET = f"{CSI}0m"
STYLES = {
    "integrated": ("✓", "32"),
    "running": ("●", "36"),
    "pending": ("○", "37"),
    "awaiting_integration": ("●", "36"),
    "integrating": ("●", "36"),
    "retrying": ("↻", "33"),
    "failed": ("✗", "31"),
    "blocked": ("⊘", "35"),
}


def format_dashboard(
    state: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]], *, now: float | None = None,
    width: int = 80, events: list[Mapping[str, str]] | None = None,
) -> str:
    """Return a complete dashboard panel from persisted state and frozen DAG tasks."""
    current = time.time() if now is None else now
    task_states = state["tasks"]
    total = len(task_states)
    integrated = sum(task["status"] == "integrated" for task in task_states.values())
    running = sum(task["status"] in {"running", "awaiting_integration", "integrating"} for task in task_states.values())
    percent = int(integrated * 100 / total) if total else 100
    elapsed = _duration(current - float(state.get("createdAt", current)))
    title = f"Task Graph  {state.get('planSlug', '?')} / {state.get('runId', '?')}"
    summary = f"{integrated}/{total} complete  |  {running} running  |  {percent}%  |  elapsed {elapsed}"
    lines = [f"{CSI}1m{title}{RESET}", summary, "─" * max(1, width)]
    compact = width < 80
    unfinished = [task_id for task_id, item in task_states.items() if item["status"] != "integrated"]
    for task_id, task_state in task_states.items():
        task = tasks.get(task_id, {})
        symbol, colour = STYLES.get(task_state["status"], ("?", "37"))
        label = f"{CSI}{colour}m{symbol} {task_id}{RESET}"
        instruction = _shorten(str(task.get("instructions", "")), max(12, width - len(task_id) - 8))
        detail = _task_detail(task_id, task_state, task, task_states, unfinished, current)
        if compact:
            lines.extend([f"{label}  {instruction}", f"  {detail}"])
        else:
            lines.append(f"{label:<20} {instruction:<{max(12, width // 2)}} {detail}")
    if events:
        lines.append("─" * max(1, width))
        lines.extend(f"{CSI}2m{_event_text(event)}{RESET}" for event in events[-3:])
    return "\n".join(lines)


class TerminalDashboard:
    """Reserve a top scrolling region and redraw the transient controller panel."""

    def __init__(
        self, output: TextIO, *, size_provider: Callable[[], tuple[int, int]] | None = None
    ) -> None:
        self.output = output
        self.size_provider = size_provider or _terminal_size
        self.events: list[dict[str, str]] = []
        self._started = False
        self._closed = False
        self._panel_height = 0

    def start(self, state: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]], *, now: float | None = None) -> None:
        self._started = True
        self._closed = False
        self.output.write("\x1b[?25l")
        self.redraw(state, tasks, now=now)

    def record_event(self, event: Mapping[str, str]) -> None:
        """Store an append-only event emitted by a real controller transition."""
        self.events.append(dict(event))

    def redraw(self, state: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]], *, now: float | None = None) -> None:
        if not self._started or self._closed:
            return
        columns, rows = self.size_provider()
        panel = format_dashboard(state, tasks, now=now, width=max(20, columns), events=self.events)
        lines = panel.splitlines()
        self._panel_height = min(len(lines), max(1, rows - 1))
        self.output.write(f"{CSI}H{CSI}J")
        self.output.write("\n".join(lines[:self._panel_height]))
        self.output.write(f"{CSI}{self._panel_height + 1};{max(self._panel_height + 1, rows)}r")
        self.output.write(f"{CSI}{self._panel_height + 1};1H")
        self.output.flush()

    def finish(self, state: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]], summary: str, *, now: float | None = None) -> None:
        self.record_event({"kind": "completion", "detail": summary})
        self.output.write(f"{CSI}{self._panel_height + 1};1H{CSI}r{summary}\n")
        self.cleanup()

    def cleanup(self) -> None:
        if not self._started or self._closed:
            return
        self.output.write(f"{CSI}r\x1b[?25h")
        self.output.flush()
        self._closed = True


def _task_detail(task_id: str, task_state: Mapping[str, Any], task: Mapping[str, Any], task_states: Mapping[str, Any], unfinished: list[str], now: float) -> str:
    status = task_state["status"]
    attempts = task_state.get("attempts", [])
    if status in {"running", "awaiting_integration", "integrating"}:
        started = attempts[-1].get("startedAt") if attempts else None
        return f"running {_duration(now - float(started))}" if started else "running"
    if status == "pending":
        dependencies = task.get("dependsOn") or [candidate for candidate in unfinished if candidate != task_id][:1]
        waiting = next((dependency for dependency in dependencies if task_states.get(dependency, {}).get("status") != "integrated"), None)
        return f"waiting for {waiting}" if waiting else "ready"
    if status in {"retrying", "failed"}:
        summary = _concise(attempts[-1].get("failureSummary", "waiting for retry") if attempts else "waiting for retry")
        return f"attempt {len(attempts)}: {summary}"
    if status == "blocked":
        return f"blocked by {task_state.get('blockedBy', 'failed dependency')}"
    return status


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def _shorten(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: max(1, limit - 1)].rstrip() + "…"


def _concise(value: str) -> str:
    return value.split(";", 1)[0]


def _event_text(event: Mapping[str, str]) -> str:
    detail = event.get("detail")
    task_id = event.get("taskId")
    action = event.get("kind", "event").replace("_", " ")
    return " ".join(part for part in (action, task_id, detail) if part)


def _terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines
