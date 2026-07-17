"""Controller-owned task-column and kanban updates."""

from __future__ import annotations

import os
from pathlib import Path


TASK_COLUMNS = ("todo", "in-progress", "done")


def move_task(plan_dir: Path, task_file: str, destination_column: str) -> None:
    """Atomically move one task brief between its canonical board columns."""
    if destination_column not in TASK_COLUMNS:
        raise ValueError(f"unknown task column: {destination_column}")
    source = next(
        (plan_dir / column / task_file for column in TASK_COLUMNS if (plan_dir / column / task_file).is_file()),
        None,
    )
    if source is None:
        raise FileNotFoundError(f"cannot find task file {task_file}")
    destination = plan_dir / destination_column / task_file
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination:
        os.replace(source, destination)


def render_kanban(plan_dir: Path) -> None:
    """Regenerate the small human board from task files, never from stale state."""
    lines = [f"# {plan_dir.name}", ""]
    for column in TASK_COLUMNS:
        lines.extend([f"## {column.replace('-', ' ').title()}", ""])
        files = sorted((plan_dir / column).glob("*.md")) if (plan_dir / column).is_dir() else []
        lines.extend(f"- {path.name}" for path in files)
        lines.append("")
    (plan_dir / "kanban.md").write_text("\n".join(lines), encoding="utf-8")
