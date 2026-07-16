"""Validate Task Graph v1 DAG artifacts deterministically."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class DagValidationError(ValueError):
    """Raised when a DAG artifact violates the Task Graph v1 contract."""


REQUIRED_TASK_FIELDS = {
    "id",
    "taskFile",
    "title",
    "instructions",
    "predictedPaths",
    "predictedSymbols",
    "dependsOn",
    "parallelSafe",
    "schedulingRationale",
}
TASK_COLUMNS = ("todo", "in-progress", "done")


def validate_dag(dag: Mapping[str, Any]) -> None:
    """Raise DagValidationError unless *dag* satisfies the v1 JSON contract."""
    if not isinstance(dag, Mapping):
        raise DagValidationError("DAG root must be an object")
    if dag.get("schemaVersion") != 1:
        raise DagValidationError("schemaVersion must be 1")
    _require_nonempty_string(dag.get("planSlug"), "planSlug")

    tasks = dag.get("tasks")
    if not isinstance(tasks, list):
        raise DagValidationError("tasks must be an array")

    ids: set[str] = set()
    filenames: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            raise DagValidationError(f"tasks[{index}] must be an object")
        missing = REQUIRED_TASK_FIELDS - task.keys()
        if missing:
            raise DagValidationError(
                f"tasks[{index}] is missing required fields: {', '.join(sorted(missing))}"
            )
        task_id = _require_nonempty_string(task["id"], f"tasks[{index}].id")
        task_file = _require_task_filename(task["taskFile"], f"tasks[{index}].taskFile")
        for field in ("title", "instructions", "schedulingRationale"):
            _require_nonempty_string(task[field], f"tasks[{index}].{field}")
        _require_string_list(task["predictedPaths"], f"tasks[{index}].predictedPaths")
        _require_string_list(task["predictedSymbols"], f"tasks[{index}].predictedSymbols")
        task_dependencies = _require_string_list(
            task["dependsOn"], f"tasks[{index}].dependsOn"
        )
        if not isinstance(task["parallelSafe"], bool):
            raise DagValidationError(f"tasks[{index}].parallelSafe must be a boolean")
        if task_id in ids:
            raise DagValidationError(f"duplicate task ID: {task_id}")
        if task_file in filenames:
            raise DagValidationError(f"duplicate task filename: {task_file}")
        ids.add(task_id)
        filenames.add(task_file)
        dependencies[task_id] = task_dependencies

    for task_id, task_dependencies in dependencies.items():
        for dependency in task_dependencies:
            if dependency not in ids:
                raise DagValidationError(
                    f"task {task_id} depends on unknown task ID: {dependency}"
                )
            if dependency == task_id:
                raise DagValidationError(f"task {task_id} depends on itself")
    _ensure_acyclic(dependencies)


def validate_dag_file(dag_path: Path, plan_dir: Path | None = None) -> None:
    """Validate a DAG JSON file and, when supplied, its plan task files."""
    try:
        dag = json.loads(dag_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DagValidationError(f"cannot read DAG JSON: {exc}") from exc

    if plan_dir is None:
        validate_dag(dag)
        return
    validate_dag_artifacts(dag, plan_dir)


def validate_dag_artifacts(dag: Mapping[str, Any], plan_dir: Path) -> None:
    """Validate an in-memory DAG against its generated plan task files."""
    validate_dag(dag)
    if dag["planSlug"] != plan_dir.name:
        raise DagValidationError(
            f"planSlug {dag['planSlug']!r} does not match plan directory {plan_dir.name!r}"
        )
    _validate_task_files(dag, plan_dir)


def _validate_task_files(dag: Mapping[str, Any], plan_dir: Path) -> None:
    id_to_file = {task["id"]: task["taskFile"] for task in dag["tasks"]}
    task_files = _find_task_files(plan_dir)
    for task in dag["tasks"]:
        task_file = task["taskFile"]
        path = task_files.get(task_file)
        if path is None:
            raise DagValidationError(f"DAG task file is missing: {task_file}")
        actual_dependencies = _parse_task_dependencies(path.read_text(encoding="utf-8"))
        expected_dependencies = {id_to_file[task_id] for task_id in task["dependsOn"]}
        if actual_dependencies != expected_dependencies:
            raise DagValidationError(
                f"Dependencies in {task_file} do not match dag.json: "
                f"expected {sorted(expected_dependencies)}, got {sorted(actual_dependencies)}"
            )


def _find_task_files(plan_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for column in TASK_COLUMNS:
        directory = plan_dir / column
        if not directory.exists():
            continue
        for path in directory.glob("*.md"):
            if path.name in found:
                raise DagValidationError(f"task filename appears in multiple columns: {path.name}")
            found[path.name] = path
    return found


def _parse_task_dependencies(content: str) -> set[str]:
    match = re.search(
        r"^##\s+Dependencies\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        content,
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise DagValidationError("task file has no ## Dependencies section")
    body = match.group("body").strip()
    if not body or body.lower() == "none":
        return set()
    return set(re.findall(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9._-]*\.md)(?![\w.-])", body))


def _ensure_acyclic(dependencies: Mapping[str, list[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise DagValidationError(f"DAG contains a cycle involving {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in dependencies:
        visit(task_id)


def _require_nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DagValidationError(f"{name} must be a non-empty string")
    return value


def _require_task_filename(value: Any, name: str) -> str:
    filename = _require_nonempty_string(value, name)
    if Path(filename).name != filename or not filename.endswith(".md"):
        raise DagValidationError(f"{name} must be a task-file basename ending in .md")
    return filename


def _require_string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DagValidationError(f"{name} must be an array of strings")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Task Graph v1 dag.json file")
    parser.add_argument("--dag", required=True, type=Path, help="path to dag.json")
    parser.add_argument(
        "--plan-dir", type=Path, help="plan directory containing todo/in-progress/done task files"
    )
    args = parser.parse_args()
    try:
        validate_dag_file(args.dag, args.plan_dir)
    except DagValidationError as exc:
        parser.error(str(exc))
    print(f"valid DAG: {args.dag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
