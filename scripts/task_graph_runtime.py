"""Durable state and immutable input handling for Task Graph runs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.dag_validation import DagValidationError, validate_dag_file
from scripts.task_graph_git import TaskGraphGit, TaskGraphGitError


STATE_SCHEMA_VERSION = 1
TASK_STATUSES = frozenset(
    {
        "pending",
        "running",
        "awaiting_integration",
        "integrating",
        "integrated",
        "retrying",
        "failed",
        "blocked",
    }
)


class TaskGraphRuntimeError(RuntimeError):
    """Raised when a persisted Task Graph run is unsafe to use."""


@dataclass(frozen=True)
class Snapshot:
    """Immutable copy of the planning inputs used by one controller run."""

    dag: dict[str, Any]
    dag_digest: str
    task_contents: dict[str, str]
    task_digests: dict[str, str]


class RunLock:
    """An advisory exclusive lock preventing competing controllers for one run."""

    def __init__(self, run_dir: Path, *, blocking: bool = False) -> None:
        self.path = run_dir / ".lock"
        self.blocking = blocking
        self._handle: Any | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(self._handle.fileno(), flags)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise TaskGraphRuntimeError(f"run is already controlled: {self.path.parent}") from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def ensure_clean_base(repository: Path, plan_slug: str) -> None:
    """Reject changes outside this plan's controller-owned runtime directory."""
    ignored_prefix = f".agent/{plan_slug}/runs/"
    try:
        clean = TaskGraphGit(repository).is_clean(ignored_prefix=ignored_prefix)
    except TaskGraphGitError as exc:
        raise TaskGraphRuntimeError(str(exc)) from exc
    if not clean:
        raise TaskGraphRuntimeError(
            "repository is dirty; commit, stash, or remove changes before starting"
        )


def create_run_snapshot(plan_dir: Path, run_dir: Path) -> Snapshot:
    """Validate then freeze the DAG and resolved task briefs for a single run."""
    dag_path = plan_dir / "dag.json"
    try:
        validate_dag_file(dag_path, plan_dir)
    except DagValidationError as exc:
        raise TaskGraphRuntimeError(f"invalid plan DAG: {exc}") from exc

    dag_bytes = dag_path.read_bytes()
    dag = json.loads(dag_bytes)
    input_dir = run_dir / "input"
    task_dir = input_dir / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "dag.json").write_bytes(dag_bytes)

    task_contents: dict[str, str] = {}
    task_digests: dict[str, str] = {}
    for task in dag["tasks"]:
        task_id = task["id"]
        source = _resolve_task_file(plan_dir, task["taskFile"])
        content = source.read_text(encoding="utf-8")
        destination = task_dir / f"{task_id}.md"
        destination.write_text(content, encoding="utf-8")
        task_contents[task_id] = content
        task_digests[task_id] = _sha256(content.encode("utf-8"))

    return Snapshot(
        dag=dag,
        dag_digest=_sha256(dag_bytes),
        task_contents=task_contents,
        task_digests=task_digests,
    )


def load_snapshot(run_dir: Path) -> Snapshot:
    """Load only the frozen inputs, never the mutable canonical plan artifacts."""
    input_dir = run_dir / "input"
    try:
        dag_bytes = (input_dir / "dag.json").read_bytes()
        dag = json.loads(dag_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskGraphRuntimeError(f"cannot load run input snapshot: {exc}") from exc

    task_contents: dict[str, str] = {}
    task_digests: dict[str, str] = {}
    for task in dag.get("tasks", []):
        task_id = task["id"]
        try:
            content = (input_dir / "tasks" / f"{task_id}.md").read_text(encoding="utf-8")
        except OSError as exc:
            raise TaskGraphRuntimeError(f"missing task input snapshot for {task_id}") from exc
        task_contents[task_id] = content
        task_digests[task_id] = _sha256(content.encode("utf-8"))
    return Snapshot(dag, _sha256(dag_bytes), task_contents, task_digests)


def create_state(
    *,
    run_id: str,
    plan_slug: str,
    repository: str,
    feature_branch: str,
    base_commit: str,
    snapshot_digest: str,
    task_digests: dict[str, str],
    max_workers: int,
    task_ids: list[str],
    git_common_dir: str,
    worker_command: str = "codex",
    base_branch: str | None = None,
) -> dict[str, Any]:
    """Create the only allowed initial task state for a controller run."""
    if max_workers < 1:
        raise TaskGraphRuntimeError("max_workers must be at least 1")
    if not worker_command.strip():
        raise TaskGraphRuntimeError("worker command must not be empty")
    common_dir = require_git_common_dir({"gitCommonDir": git_common_dir})
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "runId": run_id,
        "planSlug": plan_slug,
        "repository": repository,
        "gitCommonDir": str(common_dir),
        "featureBranch": feature_branch,
        "baseCommit": base_commit,
        "baseBranch": base_branch,
        "dagDigest": snapshot_digest,
        "taskDigests": task_digests,
        "maxWorkers": max_workers,
        "workerCommand": worker_command,
        "createdAt": time.time(),
        "controller": {},
        "tasks": {
            task_id: {"status": "pending", "attempts": [], "commitSha": None}
            for task_id in task_ids
        },
    }


def load_state(run_dir: Path) -> dict[str, Any]:
    """Load a complete persisted state file."""
    try:
        value = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskGraphRuntimeError(f"cannot load run state: {exc}") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise TaskGraphRuntimeError("invalid run state schema")
    require_git_common_dir(value)
    return value


def require_git_common_dir(state: dict[str, Any]) -> Path:
    """Return the persisted shared Git metadata path or reject an unsafe run."""
    value = state.get("gitCommonDir")
    if value is None:
        raise TaskGraphRuntimeError(
            "run state lacks gitCommonDir; start a fresh run from a clean base"
        )
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise TaskGraphRuntimeError(
            "run state has an invalid gitCommonDir; start a fresh run from a clean base"
        )
    return Path(value).resolve()


def write_state(run_dir: Path, state: dict[str, Any]) -> None:
    """Atomically replace state and flush its directory for crash durability."""
    run_dir.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=run_dir)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, run_dir / "state.json")
        directory_fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _resolve_task_file(plan_dir: Path, task_file: str) -> Path:
    matches = [plan_dir / column / task_file for column in ("todo", "in-progress", "done")]
    found = [path for path in matches if path.is_file()]
    if len(found) != 1:
        raise TaskGraphRuntimeError(f"cannot resolve exactly one task file: {task_file}")
    return found[0]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
