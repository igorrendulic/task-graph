"""Animated, in-memory demo of the real controller dashboard; Ctrl+C exits."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.task_graph_activity import WorkerActivity
from scripts.task_graph_display import TerminalDashboard


STEP_SECONDS = 5
PHASES = ('working', 'working', 'verifying', 'awaiting_integration', 'integrating', 'integrated')
ACTIVITIES = ('Running: rg dashboard scripts/', 'Editing: scripts/dashboard.py')


def preview_frame(started_at: float, now: float) -> tuple[dict[str, Any], dict[str, Any], dict[str, WorkerActivity]]:
    """Build an independent frame from elapsed time, without controller or file I/O."""
    elapsed = max(0.0, now - started_at)
    step = int(elapsed // STEP_SECONDS)
    phase = PHASES[step % len(PHASES)]
    since = started_at + step * STEP_SECONDS
    definitions = (
        ('001-dashboard', phase, 'Dashboard changes integrated'),
        ('002-parser', 'working', 'Reading parser input samples'),
        ('003-tests', 'verifying', 'Run focused regression tests'),
        ('004-api', 'awaiting_integration', 'API changes verified'),
        ('005-styles', 'integrating', 'Apply verified styles'),
        ('006-docs', 'pending', 'Document dashboard behavior'),
        ('007-repair', 'retrying', 'Repair a simulated test failure'),
        ('008-export', 'failed', 'Export task failed'),
        ('009-release', 'blocked', 'Waiting for export repair'),
        ('010-foundation', 'integrated', 'Foundation changes integrated'),
    )
    state: dict[str, Any] = {
        'planSlug': 'DEMO', 'runId': 'preview (Ctrl+C exits)',
        'createdAt': started_at, 'tasks': {},
    }
    tasks: dict[str, Any] = {}
    activity = {}
    for task_id, task_phase, instructions in definitions:
        phase_started = since if task_id == '001-dashboard' else started_at - 12
        status = 'running' if task_phase in {'working', 'verifying'} else task_phase
        attempt: dict[str, Any] = {'startedAt': phase_started, 'phaseStartedAt': phase_started}
        if task_phase == 'verifying':
            attempt.update(phase='verifying', verification={'commands': [
                {'argv': ['python3', '-m', 'unittest', 'tests.test_dashboard'], 'startedAt': phase_started},
            ]})
        if task_phase in {'failed', 'retrying'}:
            attempt['failureSummary'] = 'Simulated assertion failure'
        attempts = [] if task_phase in {'pending', 'blocked'} else [attempt]
        if task_phase == 'failed':
            attempts = [dict(attempt), attempt]
        state['tasks'][task_id] = {
            'status': status, 'phaseStartedAt': phase_started, 'attempts': attempts,
        }
        tasks[task_id] = {'instructions': instructions, 'verification': {'commands': [
            ['python3', '-m', 'unittest', 'tests.test_dashboard'],
        ]}}
        if task_phase == 'working':
            observed = WorkerActivity()
            observed.text = ACTIVITIES[step % 2] if task_id == '001-dashboard' else instructions
            observed.since = phase_started
            observed.last_event_at = now
            activity[task_id] = observed
    tasks['006-docs']['dependsOn'] = ['001-dashboard']
    tasks['009-release']['dependsOn'] = ['008-export']
    state['tasks']['009-release']['blockedBy'] = '008-export'
    return state, tasks, activity


def run_preview(dashboard: TerminalDashboard) -> None:
    started_at = time.time()
    now = started_at
    previous_step = -1
    try:
        state, tasks, dashboard.activity = preview_frame(started_at, now)
        dashboard.start(state, tasks, now=now)
        while True:
            state, tasks, dashboard.activity = preview_frame(started_at, now)
            step = int(max(0.0, now - started_at) // STEP_SECONDS)
            if step != previous_step:
                phase = PHASES[step % len(PHASES)]
                detail = ACTIVITIES[step % 2] if phase == 'working' else phase.replace('_', ' ')
                dashboard.record_event({'kind': 'SIMULATED', 'taskId': '001-dashboard', 'detail': detail})
                previous_step = step
            dashboard.redraw(state, tasks, now=now)
            time.sleep(1)
            now = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.cleanup()


def main() -> int:
    if not sys.stdout.isatty():
        print('Run the preview in a terminal or tmux pane.', file=sys.stderr)
        return 1
    run_preview(TerminalDashboard(sys.stdout))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
