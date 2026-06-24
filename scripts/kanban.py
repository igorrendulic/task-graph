#!/usr/bin/env python3
"""Manage go-mailio-server-private .agent kanban task files."""

from __future__ import annotations

import argparse
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
    if limit < 1:
        raise SystemExit("--limit must be at least 1")

    tasks = read_tasks(repo)
    completed = done_names(tasks)
    available = active_names(tasks)
    todo = sorted((task for task in tasks if task.column == "todo"), key=lambda task: task.path.name)
    startable = [task for task in todo if is_startable(task, completed)]
    batch = launch_batch(startable, limit)
    batch_names = {task.path.name for task in batch}
    remaining_startable = [task for task in startable if task.path.name not in batch_names]
    blocked = [task for task in todo if task not in startable]

    print(f"Recommended launch batch (limit {limit}):")
    if batch:
        for task in batch:
            print(f"- {task.path.name}: {task.title}")
    else:
        print("- None")

    print("\nAdditional startable parallel candidates:")
    if remaining_startable:
        for task in remaining_startable:
            print(f"- {task.path.name}: {task.title}")
    else:
        print("- None")

    print("\nSequential or blocked tasks:")
    if blocked:
        for task in blocked:
            unresolved = unresolved_dependencies(task, available)
            deps = ", ".join(unresolved) if unresolved else "waiting on in-progress dependency"
            print(f"- {task.path.name}: {task.title} ({deps})")
    else:
        print("- None")


def command_done(repo: Path, task_name: str) -> None:
    moved = move_task(repo, "in-progress", "done", task_name)
    print(f"Done: {moved}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("board", "plan", "start", "done"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--task", help="Task filename for the done command")
    parser.add_argument("--limit", type=int, default=2, help="Maximum recommended parallel launch count")
    args = parser.parse_args()

    repo = args.repo.resolve()
    if args.command == "board":
        print(f"Board: {rewrite_board(repo)}")
    elif args.command == "plan":
        command_plan(repo, args.limit)
    elif args.command == "start":
        command_start(repo)
    elif args.command == "done":
        if not args.task:
            raise SystemExit("done requires --task <filename>")
        command_done(repo, args.task)


if __name__ == "__main__":
    main()
