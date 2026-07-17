"""Generate and score an agent-produced DAG from a behavior-case fixture."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from evals.case_evaluator import evaluate_case_dag


_CODEX_PROMPT = """\
Generate Task Graph planning artifacts for this repository.

Read plan.md, inspect the repository source and current Git status, then create
.agent/<plan-slug>/todo/*.md, .agent/<plan-slug>/kanban.md, and
.agent/<plan-slug>/dag.json. You are explicitly authorized to create .agent/.
Do not modify plan.md or source files.

You must use filesystem tools to write the artifacts. Dependency naming is a
strict ID-to-filename mapping: dag.json dependsOn arrays contain task IDs,
while each task brief's ## Dependencies section contains the matching taskFile
.md filenames. For example, a DAG value of
"dependsOn": ["001-add-config-parser"] requires the dependent task brief to
list "- 001-add-config-parser.md" under ## Dependencies.

Before your final answer, verify that the canonical dag.json exists. For every
dependsOn ID, validate every task-file dependency against the referenced task's
taskFile: find the task with that ID and confirm its .md filename, rather than
its bare ID, appears in the dependent brief. If any artifact is inconsistent,
correct the artifacts and revalidate before reporting success.

Each task file must contain Type, Goal, Context, Scope, Out Of Scope,
Dependencies, Parallel, Predicted Paths and Symbols, Acceptance Criteria, and
Test Notes. The DAG must use schemaVersion 1. Each task needs id, taskFile,
title, instructions, predictedPaths, predictedSymbols, dependsOn, parallelSafe,
and schedulingRationale. Each taskFile must be only its .md filename (for
example, "001-add-schema.md"), not a path.
The root-level planSlug must be non-empty and equal the .agent/<plan-slug>/
directory name containing dag.json.

Schedule conservatively: tasks are parallel only when their complete edit
surfaces are demonstrably disjoint. Serialize shared files, symbols, contracts,
tests, generated artifacts, or uncertain surfaces. Preserve source-plan order
when serialization has no natural prerequisite. If dirty local changes overlap
a task, mark it non-parallel-safe and explain that it requires a clean base.
Each schedulingRationale must name the basis for serialization or parallel
safety. For overlapping edits, use `shared` and name the shared file, symbol,
contract, test, or artifact. For example: "It depends on task 001 because both tasks modify the shared `src/config.py` module and configuration tests." Use
`disjoint` for demonstrably separate surfaces, `uncertain` when the surface
cannot be established confidently, and `clean base` when dirty local changes
require one.
Task-file Dependencies must use the corresponding taskFile filenames for each
dag.json dependsOn task ID.\
"""


def materialize_case(case_dir: Path, repo_dir: Path) -> Path:
    """Copy a case template and plan, commit its baseline, then apply dirty edits."""
    template_dir = case_dir / "repository"
    plan_path = case_dir / "plan.md"
    if not template_dir.is_dir():
        raise ValueError(f"case has no repository template: {template_dir}")
    if not plan_path.is_file():
        raise ValueError(f"case has no plan: {plan_path}")
    if repo_dir.exists():
        raise ValueError(
            f"destination already exists: {repo_dir}. "
            "Omit --repo for normal ephemeral evals, or pass a new --repo path only when debugging."
        )

    shutil.copytree(template_dir, repo_dir)
    shutil.copy2(plan_path, repo_dir / "plan.md")
    _git(repo_dir, "init", "--quiet")
    _git(repo_dir, "config", "user.name", "Task Graph eval")
    _git(repo_dir, "config", "user.email", "task-graph-eval@example.invalid")
    _git(repo_dir, "add", "--all")
    _git(repo_dir, "commit", "--quiet", "-m", "Baseline fixture")

    for change in _read_setup(case_dir).get("dirtyChanges", []):
        _apply_dirty_change(repo_dir, change)
    return repo_dir


def run_case(
    case_dir: Path, repo_dir: Path | None = None, codex_bin: str = "codex"
) -> list[str]:
    """Generate a DAG with Codex, print it, and return case-contract violations."""
    if repo_dir is None:
        with tempfile.TemporaryDirectory(prefix=f"task-graph-eval-{case_dir.name}-") as temp:
            return _run_case_in_repo(case_dir, Path(temp) / "repo", codex_bin, keep_repo=False)
    return _run_case_in_repo(case_dir, repo_dir, codex_bin, keep_repo=True)


def _run_case_in_repo(
    case_dir: Path, repo_dir: Path, codex_bin: str, *, keep_repo: bool
) -> list[str]:
    """Generate a DAG in *repo_dir*, then score the DAG loaded into memory."""
    repo_dir = materialize_case(case_dir, repo_dir)
    generation_errors = _run_codex(repo_dir, codex_bin)
    if generation_errors:
        return generation_errors

    expected = _read_json(case_dir / "expected.json", "case expectation")
    plan_slug = expected.get("planSlug")
    if not isinstance(plan_slug, str) or not plan_slug:
        return ["case expectation planSlug must be a non-empty string"]
    dag_path = repo_dir / ".agent" / plan_slug / "dag.json"
    if not dag_path.is_file():
        message = f"Codex completed without producing the expected DAG: {dag_path}."
        if keep_repo:
            message += f" Inspect {repo_dir / 'codex-output.txt'}"
        return [message]
    try:
        dag = _read_json(dag_path, "generated DAG")
    except ValueError as exc:
        return [str(exc)]
    print(json.dumps(dag, indent=2, sort_keys=True))
    return evaluate_case_dag(case_dir, dag, dag_path.parent)


def _run_codex(repo_dir: Path, codex_bin: str) -> list[str]:
    try:
        process = subprocess.Popen(
            [
                codex_bin,
                "exec",
                "--sandbox",
                "workspace-write",
                "-C",
                str(repo_dir),
                _CODEX_PROMPT,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        return [f"Codex executable was not found: {codex_bin}"]
    if process.stdout is None:
        return ["Codex generation did not expose terminal output"]

    output: list[str] = []
    for line in process.stdout:
        print(line, end="", flush=True)
        output.append(line)
    returncode = process.wait()
    (repo_dir / "codex-output.txt").write_text(
        "".join(output),
        encoding="utf-8",
    )
    if returncode == 0:
        return []
    details = "".join(output).strip()
    message = f"Codex generation failed with exit code {returncode}"
    return [f"{message}: {details}" if details else message]


def _read_setup(case_dir: Path) -> dict[str, Any]:
    setup_path = case_dir / "setup.json"
    if not setup_path.exists():
        return {"dirtyChanges": []}
    try:
        setup = json.loads(setup_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid setup JSON: {exc}") from exc
    if not isinstance(setup, dict) or not isinstance(setup.get("dirtyChanges", []), list):
        raise ValueError("setup.json must contain a dirtyChanges array")
    return setup


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} JSON root must be an object")
    return value


def _apply_dirty_change(repo_dir: Path, change: Any) -> None:
    if not isinstance(change, dict):
        raise ValueError("each dirty change must be an object")
    relative_path = change.get("path")
    append = change.get("append")
    if not isinstance(relative_path, str) or not isinstance(append, str):
        raise ValueError("each dirty change needs string path and append values")
    destination = repo_dir / relative_path
    if not destination.is_relative_to(repo_dir):
        raise ValueError(f"dirty change escapes repository: {relative_path}")
    if not destination.exists():
        raise ValueError(f"dirty change targets a missing file: {relative_path}")
    destination.write_text(destination.read_text(encoding="utf-8") + append, encoding="utf-8")


def _git(repo_dir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and score a Task Graph DAG eval case")
    parser.add_argument("--case", required=True, type=Path, help="case directory")
    parser.add_argument(
        "--repo",
        type=Path,
        help="debug only: new destination repository to keep; omit for normal cleaned ephemeral evals",
    )
    parser.add_argument("--codex-bin", default="codex", help="Codex executable to invoke")
    args = parser.parse_args()
    try:
        errors = run_case(args.case, args.repo, args.codex_bin)
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"PASS: {args.case.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
