#!/usr/bin/env python3
"""Read-only Task Graph status dashboards and bounded exec checkpoints."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import kanban as KANBAN


WATCH_INTERVAL_SECONDS = 5


def select_entries(entries: list[dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    """Keep each task's newest run, while retaining every still-running worker."""
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for entry in entries:
        key = (str(entry["plan"]), str(entry["task"]))
        current = latest.get(key)
        candidate_key = (str(entry.get("last_activity") or ""), str(entry["run_id"]))
        current_key = (str(current.get("last_activity") or ""), str(current["run_id"])) if current else None
        if current is None or candidate_key > current_key:
            latest[key] = entry
    selected = [
        entry for entry in entries
        if entry["state"] == "RUNNING" or latest[(str(entry["plan"]), str(entry["task"]))] is entry
    ]
    visible = [entry for entry in selected if entry["state"] == "RUNNING" or entry["state"] in KANBAN.EXEC_ACTIONABLE_STATES]
    return visible, len(entries) - len(visible)


def next_action(entry: dict[str, object]) -> str:
    if entry["state"] == "RUNNING":
        return "Attach in tmux"
    if entry["state"] == "SUCCEEDED_AWAITING_REVIEW":
        return "Open report"
    if entry["state"] == "NEEDS_ATTENTION":
        return "Read report"
    return "Inspect runtime"


def render_exec(entries: list[dict[str, object]], hidden: int, elapsed: int, remaining: int) -> str:
    counts: dict[str, int] = {}
    for entry in entries:
        state = str(entry["state"])
        counts[state] = counts.get(state, 0) + 1
    summary = ", ".join(f"{state.lower()}={count}" for state, count in sorted(counts.items())) or "none"
    lines = [
        f"Task Graph exec monitor | elapsed {KANBAN.format_elapsed(elapsed)} | remaining {KANBAN.format_elapsed(remaining)}",
        f"States: {summary}",
        f"Showing {len(entries)} current item(s); {hidden} older run(s) hidden.",
    ]
    if not entries:
        return "\n".join(lines + ["No active or actionable workers."])
    lines.append("STATE                    TASK                      PLAN / RUN                 REPORT  ELAPSED   NEXT")
    for entry in entries:
        plan_run = f"{KANBAN.truncate_text(entry['plan'], 16)} / {KANBAN.truncate_text(entry['run_id'], 18)}"
        lines.append(
            f"{KANBAN.truncate_text(entry['state'], 24).ljust(24)} "
            f"{KANBAN.truncate_text(entry['task'], 25).ljust(25)} "
            f"{plan_run.ljust(26)} "
            f"{KANBAN.truncate_text(entry.get('report_status') or '-', 7).ljust(7)} "
            f"{KANBAN.format_elapsed(entry.get('elapsed')).ljust(9)} "
            f"{next_action(entry)}"
        )
    return "\n".join(lines)


def watch_status(repo: Path, plan: str | None, run_id: str | None, task_name: str | None, interval: float, *, as_json: bool = False) -> None:
    if interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    try:
        while True:
            entries = KANBAN.collect_status(repo, plan, run_id, task_name)
            if as_json:
                print(json.dumps({"tasks": entries}, indent=2, sort_keys=True))
                return
            print("\033[2J\033[H", end="")
            print("Task Graph status (Ctrl-C to stop)\n")
            KANBAN.print_status(entries)
            time.sleep(interval)
    except KeyboardInterrupt:
        print()


def watch_exec(repo: Path, plan: str | None, run_id: str | None, task_name: str | None, seconds: int, *, checkpoint: bool = False) -> int:
    if seconds <= 0:
        raise SystemExit("--seconds must be greater than zero")
    started = time.monotonic()
    deadline = started + seconds
    while True:
        entries = KANBAN.collect_status(repo, plan, run_id, task_name)
        actionable = [entry for entry in entries if entry["state"] in KANBAN.EXEC_ACTIONABLE_STATES]
        if checkpoint:
            if actionable:
                print(f"signal: {', '.join(sorted({str(entry['state']) for entry in actionable}))}")
                KANBAN.print_status(actionable)
                return 0
            if not entries:
                print("checkpoint: no active exec workers")
                return 0
        now = time.monotonic()
        remaining = deadline - now
        if not checkpoint:
            visible, hidden = select_entries(entries)
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            print(render_exec(visible, hidden, int(now - started), max(0, int(remaining))))
        if remaining <= 0:
            print(f"{'checkpoint: no actionable wake within' if checkpoint else 'monitor: finished after'} {seconds}s")
            return 124
        time.sleep(min(WATCH_INTERVAL_SECONDS, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "watch-exec"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--plan")
    parser.add_argument("--run-id")
    parser.add_argument("--task")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--seconds", type=int)
    parser.add_argument("--checkpoint", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.command == "status":
        watch_status(repo, args.plan, args.run_id, args.task, args.interval, as_json=args.json)
        return 0
    if args.seconds is None:
        raise SystemExit("watch-exec requires --seconds <positive-int>")
    return watch_exec(repo, args.plan, args.run_id, args.task, args.seconds, checkpoint=args.checkpoint)


if __name__ == "__main__":
    raise SystemExit(main())
