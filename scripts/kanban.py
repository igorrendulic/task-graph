#!/usr/bin/env python3
"""Manage project-local .agent kanban task files."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


COLUMNS = ("todo", "in-progress", "done")


@dataclass(frozen=True)
class Task:
    column: str
    path: Path
    title: str
    dependencies: tuple[str, ...]
    task_type: str


@dataclass(frozen=True)
class Schedule:
    batch: list[Task]
    remaining_startable: list[Task]
    blocked: list[Task]
    available: set[str]


def title_from_file(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    stem = re.sub(r"^\d+-", "", path.stem)
    return stem.replace("-", " ").title()


def section_text(markdown: str, heading: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$([\s\S]*?)(?=^## |\Z)",
        re.MULTILINE,
    )
    match = pattern.search(markdown)
    return match.group(1).strip() if match else ""


def parse_dependencies(path: Path) -> tuple[str, ...]:
    deps = section_text(path.read_text(encoding="utf-8"), "Dependencies")
    if not deps or deps.lower() == "none":
        return ()
    names: list[str] = []
    if "depends on:" in deps.lower():
        for line in deps.splitlines():
            if not line.lstrip().startswith("-"):
                continue
            names.extend(re.findall(r"\b\d{3}-[a-z0-9][a-z0-9-]*\.md\b", line))
            names.extend(re.findall(r"`(\d{3})`", line))

    chunks = re.split(r"(?<=[.])\s+|\n+", deps)
    for chunk in chunks:
        lowered = chunk.lower()
        if "depend" not in lowered and "can start" not in lowered and "requires" not in lowered:
            continue
        names.extend(re.findall(r"\b\d{3}-[a-z0-9][a-z0-9-]*\.md\b", chunk))
        names.extend(re.findall(r"`(\d{3})`", chunk))
        for groups in re.findall(r"\btask\s+`?(\d{3})`?\b|\btasks\s+`?(\d{3})`?\b", chunk, re.IGNORECASE):
            prefix = next((value for value in groups if value), "")
            if prefix:
                names.append(prefix)
    return tuple(dict.fromkeys(names))


def parse_task_type(path: Path) -> str:
    task_type = section_text(path.read_text(encoding="utf-8"), "Type").strip().lower()
    return task_type if task_type in {"ship", "scout"} else "ship"


def agent_dir(repo: Path) -> Path:
    directory = repo / ".agent"
    if not directory.exists():
        raise SystemExit(f"Missing agent directory: {directory}")
    return directory


def ensure_dirs(repo: Path) -> None:
    base = agent_dir(repo) / "tasks"
    for column in COLUMNS:
        (base / column).mkdir(parents=True, exist_ok=True)


def read_tasks(repo: Path) -> list[Task]:
    ensure_dirs(repo)
    tasks: list[Task] = []
    base = agent_dir(repo) / "tasks"
    for column in COLUMNS:
        for path in sorted((base / column).glob("*.md")):
            tasks.append(
                Task(
                    column=column,
                    path=path,
                    title=title_from_file(path),
                    dependencies=parse_dependencies(path),
                    task_type=parse_task_type(path),
                )
            )
    return tasks


def board_link(task: Task) -> str:
    rel = f"tasks/{task.column}/{task.path.name}"
    return f"- [{task.title}]({rel})"


def rewrite_board(repo: Path) -> Path:
    tasks = read_tasks(repo)
    by_column = {column: [] for column in COLUMNS}
    for task in tasks:
        by_column[task.column].append(task)

    lines = ["# Kanban Board", ""]
    headings = {"todo": "TODO", "in-progress": "IN PROGRESS", "done": "DONE"}
    for column in COLUMNS:
        lines.extend([f"## {headings[column]}", ""])
        column_tasks = by_column[column]
        if column_tasks:
            lines.extend(board_link(task) for task in column_tasks)
        else:
            lines.append("_None_")
        lines.append("")

    path = agent_dir(repo) / "kanban.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def done_names(tasks: list[Task]) -> set[str]:
    names: set[str] = set()
    for task in tasks:
        if task.column == "done":
            names.add(task.path.name)
            names.add(task.path.name[:3])
    return names


def active_names(tasks: list[Task]) -> set[str]:
    names: set[str] = set()
    for task in tasks:
        if task.column in {"in-progress", "done"}:
            names.add(task.path.name)
            names.add(task.path.name[:3])
    return names


def run_dir(repo: Path, run_id: str) -> Path:
    return agent_dir(repo) / "runs" / run_id


def progress_path(repo: Path, run_id: str) -> Path:
    return run_dir(repo, run_id) / "progress.md"


def completed_names_from_ledger(repo: Path, run_id: str | None) -> set[str]:
    if not run_id:
        return set()
    path = progress_path(repo, run_id)
    if not path.exists():
        return set()

    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"-\s+(\d{3}-[a-z0-9][a-z0-9-]*\.md):\s+complete\b", line)
        if match:
            names.add(match.group(1))
            names.add(match.group(1)[:3])
    return names


def is_startable(task: Task, completed: set[str]) -> bool:
    return all(dep in completed for dep in task.dependencies)


def task_depends_on(left: Task, right: Task) -> bool:
    return right.path.name in left.dependencies or right.path.name[:3] in left.dependencies


def can_run_in_parallel(left: Task, right: Task) -> bool:
    return not task_depends_on(left, right) and not task_depends_on(right, left)


def parallel_candidates(todo: list[Task], completed: set[str], selected: Task) -> list[Task]:
    candidates = []
    for task in todo:
        if task.path == selected.path:
            continue
        if not is_startable(task, completed):
            continue
        if not can_run_in_parallel(selected, task):
            continue
        candidates.append(task)
    return candidates


def unresolved_dependencies(task: Task, available: set[str]) -> tuple[str, ...]:
    return tuple(dep for dep in task.dependencies if dep not in available)


def launch_batch(startable: list[Task], limit: int) -> list[Task]:
    selected: list[Task] = []
    for task in startable:
        if len(selected) >= limit:
            break
        if all(can_run_in_parallel(task, chosen) for chosen in selected):
            selected.append(task)
    return selected


def schedule_tasks(repo: Path, limit: int, run_id: str | None = None) -> Schedule:
    if limit < 1:
        raise SystemExit("--limit must be at least 1")

    tasks = read_tasks(repo)
    ledger_completed = completed_names_from_ledger(repo, run_id)
    completed = done_names(tasks) | ledger_completed
    available = active_names(tasks) | ledger_completed
    todo = sorted((task for task in tasks if task.column == "todo"), key=lambda task: task.path.name)
    launchable_todo = [
        task
        for task in todo
        if task.path.name not in ledger_completed and task.path.name[:3] not in ledger_completed
    ]
    startable = [task for task in launchable_todo if is_startable(task, completed)]
    batch = launch_batch(startable, limit)
    batch_names = {task.path.name for task in batch}
    remaining_startable = [task for task in startable if task.path.name not in batch_names]
    blocked = [task for task in launchable_todo if task not in startable]
    return Schedule(batch=batch, remaining_startable=remaining_startable, blocked=blocked, available=available)


def task_payload(task: Task, available: set[str]) -> dict[str, object]:
    return {
        "file": task.path.name,
        "title": task.title,
        "type": task.task_type,
        "column": task.column,
        "dependencies": list(task.dependencies),
        "unresolved_dependencies": list(unresolved_dependencies(task, available)),
    }


def print_schedule(schedule: Schedule, limit: int) -> None:
    print(f"Recommended launch batch (limit {limit}):")
    if schedule.batch:
        for task in schedule.batch:
            print(f"- {task.path.name}: {task.title}")
    else:
        print("- None")

    print("\nAdditional startable parallel candidates:")
    if schedule.remaining_startable:
        for task in schedule.remaining_startable:
            print(f"- {task.path.name}: {task.title}")
    else:
        print("- None")

    print("\nSequential or blocked tasks:")
    if schedule.blocked:
        for task in schedule.blocked:
            unresolved = unresolved_dependencies(task, schedule.available)
            deps = ", ".join(unresolved) if unresolved else "waiting on in-progress dependency"
            print(f"- {task.path.name}: {task.title} ({deps})")
    else:
        print("- None")


def print_schedule_json(schedule: Schedule, limit: int) -> None:
    payload = {
        "limit": limit,
        "recommended_launch_batch": [task_payload(task, schedule.available) for task in schedule.batch],
        "additional_startable_parallel_candidates": [
            task_payload(task, schedule.available) for task in schedule.remaining_startable
        ],
        "sequential_or_blocked_tasks": [task_payload(task, schedule.available) for task in schedule.blocked],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def ensure_run_dirs(repo: Path, run_id: str) -> Path:
    directory = run_dir(repo, run_id)
    for name in ("briefs", "reports", "reviews"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    path = directory / "progress.md"
    if not path.exists():
        path.write_text(f"# Task Graph Run {run_id}\n\n", encoding="utf-8")
    return directory


def append_progress(repo: Path, run_id: str, task: Task, status: str) -> None:
    path = progress_path(repo, run_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {task.path.name}: {status} (type {task.task_type})\n")


def move_task(repo: Path, source_column: str, dest_column: str, task_name: str | None = None) -> Path:
    base = agent_dir(repo) / "tasks"
    source = base / source_column
    dest = base / dest_column
    dest.mkdir(parents=True, exist_ok=True)

    matches = sorted(source.glob(task_name or "*.md"))
    if not matches:
        label = task_name or "*.md"
        raise SystemExit(f"No matching task in {source}: {label}")
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise SystemExit(f"Task name is ambiguous: {names}")

    target = dest / matches[0].name
    if target.exists():
        raise SystemExit(f"Destination already exists: {target}")
    shutil.move(str(matches[0]), str(target))
    rewrite_board(repo)
    return target


def command_start(repo: Path) -> None:
    tasks = read_tasks(repo)
    completed = done_names(tasks)
    todo = sorted((task for task in tasks if task.column == "todo"), key=lambda task: task.path.name)
    startable = [task for task in todo if is_startable(task, completed)]
    if not startable:
        raise SystemExit("No startable todo task. Check Dependencies sections and done tasks.")

    selected = startable[0]
    parallels = parallel_candidates(todo, completed, selected)
    moved = move_task(repo, "todo", "in-progress", selected.path.name)
    print(f"Started: {moved}")
    if parallels:
        print("Also startable in parallel:")
        for task in parallels:
            print(f"- {task.path.name}: {task.title}")


def command_plan(repo: Path, limit: int) -> None:
    schedule = schedule_tasks(repo, limit)
    print_schedule(schedule, limit)


def command_reserve(repo: Path, limit: int, run_id: str) -> None:
    ensure_run_dirs(repo, run_id)
    schedule = schedule_tasks(repo, limit, run_id)

    print(f"Reserved launch batch (limit {limit}):")
    if not schedule.batch:
        print("- None")
        return

    for task in schedule.batch:
        moved = move_task(repo, "todo", "in-progress", task.path.name)
        append_progress(repo, run_id, task, "in-progress")
        print(f"- {task.path.name}: {task.title} -> {moved}")


def command_done(repo: Path, task_name: str) -> None:
    moved = move_task(repo, "in-progress", "done", task_name)
    print(f"Done: {moved}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("board", "plan", "reserve", "start", "done"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--task", help="Task filename for the done command")
    parser.add_argument("--limit", type=int, default=5, help="Maximum recommended parallel launch count")
    parser.add_argument("--run-id", help="Run identifier for run ledger commands")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON where supported")
    args = parser.parse_args()

    repo = args.repo.resolve()
    if args.command == "board":
        print(f"Board: {rewrite_board(repo)}")
    elif args.command == "plan":
        schedule = schedule_tasks(repo, args.limit)
        if args.json:
            print_schedule_json(schedule, args.limit)
        else:
            print_schedule(schedule, args.limit)
    elif args.command == "reserve":
        if not args.run_id:
            raise SystemExit("reserve requires --run-id <id>")
        command_reserve(repo, args.limit, args.run_id)
    elif args.command == "start":
        command_start(repo)
    elif args.command == "done":
        if not args.task:
            raise SystemExit("done requires --task <filename>")
        command_done(repo, args.task)


if __name__ == "__main__":
    main()
