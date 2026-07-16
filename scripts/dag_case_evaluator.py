"""Evaluate agent-produced Task Graph artifacts against a behavior case."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from scripts.dag_validation import (
        DagValidationError,
        validate_dag_artifacts,
        validate_dag_file,
    )
except ModuleNotFoundError:  # Direct execution: python3 scripts/dag_case_evaluator.py
    from dag_validation import DagValidationError, validate_dag_artifacts, validate_dag_file


def evaluate_case(case_dir: Path, artifacts_dir: Path) -> list[str]:
    """Return behavior-contract violations for artifacts generated for *case_dir*."""
    dag_path = artifacts_dir / "dag.json"
    try:
        validate_dag_file(dag_path, artifacts_dir)
        actual = _read_json(dag_path, "DAG")
    except DagValidationError as exc:
        return [str(exc)]
    return evaluate_case_dag(case_dir, actual, artifacts_dir)


def evaluate_case_dag(case_dir: Path, dag: dict[str, Any], artifacts_dir: Path) -> list[str]:
    """Return behavior-contract violations for an in-memory generated DAG."""
    expected = _read_json(case_dir / "expected.json", "case expectation")
    try:
        validate_dag_artifacts(dag, artifacts_dir)
    except DagValidationError as exc:
        return [str(exc)]
    errors: list[str] = []
    expected_slug = expected.get("planSlug")
    if dag["planSlug"] != expected_slug:
        errors.append(
            f"planSlug: expected {expected_slug!r}, got {dag['planSlug']!r}"
        )

    expected_tasks = expected.get("tasks")
    if not isinstance(expected_tasks, dict):
        return errors + ["case expectation tasks must be an object keyed by task ID"]
    actual_tasks = {task["id"]: task for task in dag["tasks"]}
    if set(actual_tasks) != set(expected_tasks):
        errors.append(
            f"task IDs: expected {sorted(expected_tasks)}, got {sorted(actual_tasks)}"
        )

    for task_id, task_expectation in expected_tasks.items():
        task = actual_tasks.get(task_id)
        if task is None:
            continue
        if not isinstance(task_expectation, dict):
            errors.append(f"case expectation for {task_id} must be an object")
            continue
        for field in ("dependsOn", "parallelSafe"):
            if field in task_expectation and task[field] != task_expectation[field]:
                errors.append(
                    f"{task_id}.{field}: expected {task_expectation[field]!r}, got {task[field]!r}"
                )
        rationale = task["schedulingRationale"].lower()
        for phrase in task_expectation.get("rationaleContains", []):
            if phrase.lower() not in rationale:
                errors.append(
                    f"{task_id}.schedulingRationale must contain {phrase!r}"
                )
    return errors


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DagValidationError(f"cannot read {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DagValidationError(f"{label} JSON root must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Task Graph artifacts against a behavior case")
    parser.add_argument("--case", required=True, type=Path, help="case directory with expected.json")
    parser.add_argument(
        "--artifacts", required=True, type=Path, help="agent-produced .agent/<plan-slug> directory"
    )
    args = parser.parse_args()
    errors = evaluate_case(args.case, args.artifacts)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"PASS: {args.case.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
