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
    resolved_tasks, resolution_errors = _resolve_expected_tasks(
        expected_tasks, actual_tasks
    )
    if resolution_errors:
        return errors + resolution_errors

    for task_id, task_expectation in expected_tasks.items():
        if not isinstance(task_expectation, dict):
            errors.append(f"case expectation for {task_id} must be an object")
            continue
        task = resolved_tasks[task_id]
        for field in ("dependsOn", "parallelSafe"):
            if field not in task_expectation:
                continue
            expected_value = task_expectation[field]
            if field == "dependsOn":
                try:
                    expected_value = [
                        resolved_tasks[dependency]["id"]
                        for dependency in expected_value
                    ]
                except KeyError as exc:
                    errors.append(
                        f"{task_id}.dependsOn references unknown expected task ID: {exc.args[0]}"
                    )
                    continue
            if task[field] != expected_value:
                errors.append(
                    f"{task_id}.{field}: expected {expected_value!r}, got {task[field]!r}"
                )
        rationale = task["schedulingRationale"].lower()
        for phrase in task_expectation.get("rationaleContains", []):
            if phrase.lower() not in rationale:
                errors.append(
                    f"{task_id}.schedulingRationale must contain {phrase!r}"
                )
    return errors


def _resolve_expected_tasks(
    expected_tasks: dict[str, Any], actual_tasks: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Match fixture task aliases to generated DAG tasks."""
    if not any(
        isinstance(expectation, dict) and "titleContains" in expectation
        for expectation in expected_tasks.values()
    ):
        if set(actual_tasks) != set(expected_tasks):
            return {}, [
                f"task IDs: expected {sorted(expected_tasks)}, got {sorted(actual_tasks)}"
            ]
        return {task_id: actual_tasks[task_id] for task_id in expected_tasks}, []

    resolved: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    matched_ids: set[str] = set()
    for task_id, expectation in expected_tasks.items():
        if not isinstance(expectation, dict):
            errors.append(f"case expectation for {task_id} must be an object")
            continue
        has_title_matcher = "titleContains" in expectation
        title_contains = expectation.get("titleContains")
        if not has_title_matcher:
            candidates = [actual_tasks[task_id]] if task_id in actual_tasks else []
        elif not isinstance(title_contains, str) or not title_contains.strip():
            errors.append(f"{task_id}.titleContains must be a non-empty string")
            continue
        else:
            needle = title_contains.lower()
            candidates = [
                task
                for task in actual_tasks.values()
                if needle in task["title"].lower()
            ]
        if not candidates:
            label = "task ID" if not has_title_matcher else "titleContains"
            expected_value = task_id if not has_title_matcher else title_contains
            errors.append(f"{task_id}.{label} did not match a generated task: {expected_value!r}")
            continue
        if len(candidates) > 1:
            errors.append(
                f"{task_id}.titleContains matched multiple generated tasks: "
                f"{', '.join(task['id'] for task in candidates)}"
            )
            continue
        task = candidates[0]
        if task["id"] in matched_ids:
            errors.append(
                f"{task_id} resolves to generated task already matched by another expectation: "
                f"{task['id']}"
            )
            continue
        resolved[task_id] = task
        matched_ids.add(task["id"])
    unmatched_ids = sorted(set(actual_tasks) - matched_ids)
    if not errors and unmatched_ids:
        errors.append(
            f"generated task IDs were not matched by the case: {', '.join(unmatched_ids)}"
        )
    return resolved, errors


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
