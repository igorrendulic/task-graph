"""ANSI rendering for the Task Graph controller's transient terminal dashboard."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from scripts.task_graph_activity import WorkerActivity, terminal_text


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


def _phase(task_state: Mapping[str, Any]) -> str:
    status = task_state['status']
    attempts = task_state.get('attempts', [])
    if status == 'running':
        return 'verifying' if attempts and attempts[-1].get('phase') == 'verifying' else 'working'
    return {'pending': 'waiting', 'awaiting_integration': 'awaiting integration'}.get(status, status)


def _ordered_tasks(state: Mapping[str, Any]) -> list[tuple[str, Any]]:
    priority = {'running': 0, 'integrating': 0, 'awaiting_integration': 0,
                'failed': 1, 'retrying': 2, 'pending': 3, 'blocked': 4, 'integrated': 5}
    return sorted(state['tasks'].items(), key=lambda pair: (priority.get(pair[1]['status'], 6), pair[0]))


def format_dashboard(
    state: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]], *, now: float | None = None,
    width: int = 80, activity: Mapping[str, WorkerActivity] | None = None,
) -> str:
    """Render stable task rows; observed activity never changes scheduling state."""
    current = time.time() if now is None else now
    activity = activity or {}
    task_states = state['tasks']
    total = len(task_states)
    phases = [_phase(task) for task in task_states.values()]
    integrated = phases.count('integrated')
    elapsed = _duration(current - float(state.get('createdAt', current)))
    title = _shorten(f"Task Graph  {state.get('planSlug', '?')} / {state.get('runId', '?')}", width)
    counts = [f'{integrated}/{total} integrated']
    for phase in ('working', 'verifying', 'awaiting integration', 'integrating', 'waiting', 'retrying', 'failed', 'blocked'):
        if phases.count(phase):
            counts.append(f'{phases.count(phase)} {phase}')
    # Keep elapsed time visible even when task counts need truncation.
    clock = f'elapsed {elapsed}'
    summary = _shorten(' | '.join(counts), max(1, width - len(clock) - 3)) + ' | ' + clock
    compact = width < 80
    headings = 'TASK / PHASE / AGE' if compact else f"  {'TASK':<22} {'PHASE':<20} {'AGE':>8}  CURRENT ACTIVITY"
    lines = [f'{CSI}1m{title}{RESET}', _shorten(summary, width), headings[:width]]
    for task_id, task_state in _ordered_tasks(state):
        task = tasks.get(task_id, {})
        phase = _phase(task_state)
        symbol, colour = STYLES.get(task_state['status'], ('?', '37'))
        detail, since = _task_detail(task_state, task, task_states, activity.get(task_id), current)
        age = _duration(current - since) if since is not None else '—'
        if compact:
            label = _shorten(f'{symbol} {task_id} {phase} {age}', width)
            lines.extend([f'{CSI}{colour}m{label}{RESET}', f'  {_shorten(detail, max(1, width - 2))}'])
        else:
            label = f'{symbol} {_shorten(task_id, 22):<22} {phase:<20} {age:>8}'
            lines.append(f'{CSI}{colour}m{label}{RESET}  {_shorten(detail, max(1, width - len(label) - 2))}')
    return '\n'.join(lines)


class TerminalDashboard:
    """Reserve a top scrolling region and redraw the transient controller panel."""

    def __init__(
        self, output: TextIO, *, size_provider: Callable[[], tuple[int, int]] | None = None
    ) -> None:
        self.output = output
        self.size_provider = size_provider or _terminal_size
        self._started = False
        self._closed = False
        self._panel_height = 0
        self._last_size: tuple[int, int] | None = None
        self.recent_events: deque[str] = deque(maxlen=100)
        self.activity: Mapping[str, WorkerActivity] = {}

    def start(self, state: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]], *, now: float | None = None) -> None:
        self._started = True
        self._closed = False
        self.output.write("\x1b[?25l")
        self.redraw(state, tasks, now=now)

    def record_event(self, event: Mapping[str, str]) -> None:
        """Append a real lifecycle transition in the reserved scrollback region."""
        if not self._started or self._closed:
            return
        text = terminal_text(_event_text(event))[:2000]
        stamped = f"{time.strftime('%H:%M:%S')}  {text}"
        self.recent_events.append(stamped)
        self.output.write(f"{CSI}2m{stamped}{RESET}\n")
        self.output.flush()

    def redraw(self, state: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]], *, now: float | None = None) -> None:
        if not self._started or self._closed:
            return
        columns, rows = self.size_provider()
        lines = self._visible_panel(state, tasks, max(20, columns), max(1, rows), now)
        previous_height = self._panel_height
        self._panel_height = len(lines)
        self.output.write(f"{CSI}r")
        if self._last_size != (columns, rows):
            # tmux reflows existing rows on resize; clear stale panel fragments.
            self.output.write(f'{CSI}2J')
            self._last_size = (columns, rows)
        for row in range(1, min(max(1, rows), max(previous_height, self._panel_height)) + 1):
            self.output.write(f"{CSI}{row};1H{CSI}2K")
        for row, line in enumerate(lines, start=1):
            self.output.write(f"{CSI}{row};1H{line}")
        scroll_top = min(self._panel_height + 1, max(1, rows))
        self.output.write(f"{CSI}{scroll_top};{max(scroll_top, rows)}r")
        self.output.write(f"{CSI}{max(scroll_top, rows)};1H")
        self.output.flush()

    def _visible_panel(
        self,
        state: Mapping[str, Any],
        tasks: Mapping[str, Mapping[str, Any]],
        columns: int,
        rows: int,
        now: float | None,
    ) -> list[str]:
        """Prioritize active/failing tasks without rotating them on refresh."""
        lines = format_dashboard(state, tasks, now=now, width=columns, activity=self.activity).splitlines()
        maximum_height = max(1, rows - 1)
        header, task_lines = lines[:3], lines[3:]
        task_height = 2 if columns < 80 else 1
        if maximum_height <= len(header):
            return header[:maximum_height]
        # Reserve recent events only after room for at least three task rows.
        event_room = max(0, maximum_height - len(header) - min(len(task_lines), 3 * task_height) - 1)
        event_count = min(3, len(self.recent_events), max(0, event_room - 1))
        event_lines = (['RECENT EVENTS'] + [_shorten(e, columns) for e in list(self.recent_events)[-event_count:]]) if event_count else []
        capacity = maximum_height - len(header) - len(event_lines)
        hidden = len(task_lines) > capacity
        if hidden:
            capacity -= 1
        capacity = max(0, capacity - capacity % task_height)
        visible = task_lines[:capacity]
        notice = []
        if hidden:
            hidden_tasks = _ordered_tasks(state)[len(visible) // task_height:]
            completed = sum(task['status'] == 'integrated' for _, task in hidden_tasks)
            notice = [_shorten(f'{len(hidden_tasks)} hidden ({completed} integrated); tmux prefix+w: worker logs', columns)]
        return [*header, *visible, *notice, *event_lines]

    def finish(self, state: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]], summary: str, *, now: float | None = None) -> None:
        self.record_event({"kind": "completion", "detail": summary})
        self.cleanup()

    def cleanup(self) -> None:
        if not self._started or self._closed:
            return
        self.output.write(f"{CSI}r\x1b[?25h")
        self.output.flush()
        self._closed = True


def _task_detail(task_state: Mapping[str, Any], task: Mapping[str, Any],
                 task_states: Mapping[str, Any], activity: WorkerActivity | None,
                 now: float) -> tuple[str, float | None]:
    phase = _phase(task_state)
    attempts = task_state.get('attempts', [])
    attempt = attempts[-1] if attempts else {}
    since = task_state.get('phaseStartedAt', attempt.get('startedAt'))
    if phase == 'verifying':
        verification = attempt.get('verification', {})
        commands = verification.get('commands', [])
        if attempt.get('verificationWaiting'):
            return 'Waiting for previous verification process', attempt.get('phaseStartedAt')
        if commands:
            command = commands[-1]
            total = len(task.get('verification', {}).get('commands', []))
            return f"Check {len(commands)}/{total}: {shlex.join(command['argv'])}", command['startedAt']
        return 'Preparing verification', attempt.get('phaseStartedAt')
    if phase == 'working':
        detail = activity.text if activity and activity.text else str(task.get('instructions', 'Waiting for worker output'))
        if activity and activity.since is not None:
            since = activity.since
        if activity and activity.last_event_at is not None and now - activity.last_event_at >= 60:
            live = 'alive; ' if attempt.get('workerAlive') else ''
            detail += f' ({live}last event {_duration(now - activity.last_event_at)} ago)'
        if len(attempts) > 1:
            detail = f'repair {len(attempts)}/2: {detail}'
        return detail, since
    if phase == 'waiting':
        waiting = [dependency for dependency in task.get('dependsOn', [])
                   if task_states.get(dependency, {}).get('status') != 'integrated']
        return ('waiting for ' + ', '.join(waiting) if waiting else 'ready'), None
    if phase in {'retrying', 'failed'}:
        reason = _concise(attempt.get('failureSummary', 'waiting for retry'))
        return f'attempt {len(attempts)}/2: {reason}', since
    if phase == 'blocked':
        return f"blocked by {task_state.get('blockedBy', 'failed dependency')}", None
    if phase == 'awaiting integration':
        return 'Verified commit queued for integration', since
    if phase == 'integrating':
        return 'Cherry-picking verified commit', since
    return str(task.get('instructions', phase)), None


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def _shorten(value: str, limit: int) -> str:
    compact = terminal_text(value)
    return compact if len(compact) <= limit else compact[: max(1, limit - 1)].rstrip() + "…"


def _concise(value: str) -> str:
    return value.splitlines()[0].split(";", 1)[0] if value else ""


def _event_text(event: Mapping[str, str]) -> str:
    detail = event.get("detail")
    task_id = event.get("taskId")
    action = event.get("kind", "event").replace("_", " ")
    return " ".join(part for part in (action, task_id, detail) if part)


def _terminal_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines
