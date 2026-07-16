#!/usr/bin/env python3
"""Manage project-local .agent kanban task files."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from contextlib import contextmanager


COLUMNS = ("todo", "in-progress", "done")
DELIVERY_MODES = frozenset({"no-mistakes", "direct-pr", "local-only"})
EXEC_ACTIONABLE_STATES = frozenset({"SUCCEEDED_AWAITING_REVIEW", "NEEDS_ATTENTION", "STALE", "UNKNOWN"})
EXEC_WATCH_INTERVAL_SECONDS = 5
PLAN_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
RUNTIME_FIELDS = {
    "version", "plan", "run_id", "task", "session", "pid", "command", "branch",
    "window", "role", "worktree", "base_commit", "brief", "report", "log", "started_at", "finished_at", "exit_code",
}
LAUNCH_STATES = frozenset({"intent", "launched", "conflicted", "failed"})
_mutation_local = threading.local()


class TmuxTargetConflict(SystemExit):
    """A named tmux target appeared while a caller was creating it."""


# Repository-wide controller protocol.  The board remains plan-scoped, but a
# single controller owns mutations for the whole repository.  JSONL is used
# for the request/result journals so an interrupted controller can replay an
# operation by its stable id without inventing a second transition.
FLEET_STATE_VERSION = 1


def new_operation_id() -> str:
    """Allocate a fresh operation identity unless a retry explicitly supplies one."""
    return uuid.uuid4().hex


def fleet_state_path(repo: Path) -> Path:
    return agent_dir(repo) / "state" / "fleet-controller.json"


def fleet_mutation_lock_path(repo: Path) -> Path:
    return agent_dir(repo) / "state" / "fleet-mutation.lock"


def controller_requests_path(repo: Path) -> Path:
    return agent_dir(repo) / "state" / "controller-requests.jsonl"


def controller_results_path(repo: Path) -> Path:
    return agent_dir(repo) / "state" / "controller-results.jsonl"


def controller_claims_path(repo: Path) -> Path:
    return agent_dir(repo) / "state" / "controller-operation-claims.json"


@contextmanager
def fleet_mutation_lock(repo: Path):
    path = fleet_mutation_lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_fleet_state(repo: Path, state: dict[str, object]) -> None:
    state = {**state, "version": FLEET_STATE_VERSION, "updated_at": utc_now()}
    write_atomic(fleet_state_path(repo), json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_fleet_state(repo: Path) -> dict[str, object] | None:
    try:
        state = json.loads(fleet_state_path(repo).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid fleet controller state: {error.msg}") from None
    if not isinstance(state, dict) or state.get("version") != FLEET_STATE_VERSION:
        raise SystemExit("Invalid fleet controller state")
    return state


def fleet_controller_is_live(repo: Path) -> bool:
    state = load_fleet_state(repo)
    return bool(state and state.get("lifecycle") == "running" and isinstance(state.get("session"), str)
                and tmux_session_exists(str(state["session"])))


def load_controller_requests(repo: Path) -> list[dict[str, object]]:
    path = controller_requests_path(repo)
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SupervisionStateCorruption(path, error.msg, line_number) from None
        if not isinstance(record, dict) or not isinstance(record.get("operation_id"), str):
            raise SupervisionStateCorruption(path, "invalid controller request", line_number)
        records.append(record)
    return records


def load_controller_results(repo: Path) -> dict[str, dict[str, object]]:
    path = controller_results_path(repo)
    if not path.exists():
        return {}
    results: dict[str, dict[str, object]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            result = json.loads(line)
        except json.JSONDecodeError as error:
            raise SupervisionStateCorruption(path, error.msg, line_number) from None
        if not isinstance(result, dict) or not isinstance(result.get("operation_id"), str):
            raise SupervisionStateCorruption(path, "invalid controller result", line_number)
        results[str(result["operation_id"])] = result
    return results


def load_controller_claims(repo: Path) -> dict[str, dict[str, object]]:
    path = controller_claims_path(repo)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise SupervisionStateCorruption(path, error.msg) from None
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, dict) for key, item in value.items()):
        raise SupervisionStateCorruption(path, "expected an operation-id object")
    return value


def claim_controller_operation(repo: Path, request: dict[str, object]) -> dict[str, object]:
    """Write the replay marker before a controller invokes a mutator."""
    operation_id = str(request["operation_id"])
    with fleet_mutation_lock(repo):
        claims = load_controller_claims(repo)
        claim = claims.get(operation_id)
        if claim is not None:
            return claim
        claim = {"operation_id": operation_id, "state": "claimed", "request": request, "claimed_at": utc_now()}
        if request.get("command") == "start":
            plan = request.get("plan")
            if isinstance(plan, str):
                tasks = read_tasks(repo, plan)
                completed = done_names(tasks)
                startable = sorted(
                    (task for task in tasks if task.column == "todo" and is_startable(task, completed)),
                    key=lambda task: task.path.name,
                )
                if startable:
                    claim["intent_task"] = startable[0].path.name
        if request.get("command") == "reserve":
            plan, arguments = request.get("plan"), request.get("arguments")
            if isinstance(plan, str) and isinstance(arguments, dict):
                schedule = schedule_tasks(repo, plan, int(arguments.get("limit", 5)), str(arguments.get("run_id") or "") or None)
                claim["intent_tasks"] = [task.path.name for task in schedule.batch]
                claim["intent_policy"] = {"mode": arguments.get("delivery_mode"), "yolo": bool(arguments.get("yolo", False))}
        claims[operation_id] = claim
        write_atomic(controller_claims_path(repo), json.dumps(claims, indent=2, sort_keys=True) + "\n")
        return claim


def submit_controller_request(repo: Path, plan: str, command: str, arguments: dict[str, object], *, operation_id: str) -> dict[str, object]:
    """Durably queue a mutation; the caller never performs it itself."""
    if not operation_id.strip():
        raise SystemExit("--operation-id must not be empty")
    with fleet_mutation_lock(repo):
        if not fleet_controller_is_live(repo):
            raise SystemExit("Controller migration required: no live repository controller; start controller.py first")
        results = load_controller_results(repo)
        if operation_id in results:
            return results[operation_id]
        for request in load_controller_requests(repo):
            if request["operation_id"] == operation_id:
                return request
        request: dict[str, object] = {
            "operation_id": operation_id, "plan": plan, "command": command,
            "arguments": arguments, "state": "queued", "requested_at": utc_now(),
        }
        append_jsonl_durable(controller_requests_path(repo), [request])
        return request


def record_controller_result(repo: Path, operation_id: str, *, state: str, detail: str = "") -> dict[str, object]:
    with fleet_mutation_lock(repo):
        existing = load_controller_results(repo).get(operation_id)
        if existing is not None:
            return existing
        result: dict[str, object] = {"operation_id": operation_id, "state": state, "detail": detail, "completed_at": utc_now()}
        append_jsonl_durable(controller_results_path(repo), [result])
        claims = load_controller_claims(repo)
        if operation_id in claims:
            claims[operation_id]["state"] = state
            claims[operation_id]["completed_at"] = result["completed_at"]
            write_atomic(controller_claims_path(repo), json.dumps(claims, indent=2, sort_keys=True) + "\n")
        return result


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


class SupervisionStateCorruption(RuntimeError):
    """Durable coordination state cannot be interpreted safely."""

    def __init__(self, path: Path, detail: str, line: int | None = None) -> None:
        self.path = path
        self.line = line
        self.detail = detail
        location = f"{path}:{line}" if line is not None else str(path)
        super().__init__(f"Supervision state corruption at {location}: {detail}")


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


def plan_dir(repo: Path, plan: str) -> Path:
    if not PLAN_SLUG.fullmatch(plan):
        raise SystemExit("--plan must be a lowercase kebab-case slug")
    return agent_dir(repo) / plan


def ensure_dirs(repo: Path, plan: str) -> None:
    base = plan_dir(repo, plan)
    for column in COLUMNS:
        (base / column).mkdir(parents=True, exist_ok=True)


def read_tasks(repo: Path, plan: str) -> list[Task]:
    ensure_dirs(repo, plan)
    tasks: list[Task] = []
    base = plan_dir(repo, plan)
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


def read_tasks_readonly(repo: Path, plan: str) -> list[Task]:
    """Read task files without creating columns or regenerating the board."""
    base = plan_dir(repo, plan)
    tasks: list[Task] = []
    for column in COLUMNS:
        directory = base / column
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            tasks.append(
                Task(column, path, title_from_file(path), parse_dependencies(path), parse_task_type(path))
            )
    return tasks


def board_link(task: Task) -> str:
    rel = f"{task.column}/{task.path.name}"
    return f"- [{task.title}]({rel})"


def rewrite_board(repo: Path, plan: str) -> Path:
    tasks = read_tasks(repo, plan)
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

    path = plan_dir(repo, plan) / "kanban.md"
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


def run_dir(repo: Path, plan: str, run_id: str) -> Path:
    return plan_dir(repo, plan) / "runs" / run_id


def progress_path(repo: Path, plan: str, run_id: str) -> Path:
    return run_dir(repo, plan, run_id) / "progress.md"


def completed_names_from_ledger(repo: Path, plan: str, run_id: str | None) -> set[str]:
    if not run_id:
        return set()
    path = progress_path(repo, plan, run_id)
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


def schedule_tasks(repo: Path, plan: str, limit: int, run_id: str | None = None) -> Schedule:
    if limit < 1:
        raise SystemExit("--limit must be at least 1")

    tasks = read_tasks(repo, plan)
    ledger_completed = completed_names_from_ledger(repo, plan, run_id)
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


def ensure_run_dirs(repo: Path, plan: str, run_id: str) -> Path:
    directory = run_dir(repo, plan, run_id)
    for name in ("briefs", "reports", "reviews", "diffs", "logs", "runtime"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    path = directory / "progress.md"
    if not path.exists():
        path.write_text(f"# Task Graph Run {run_id}\n\n", encoding="utf-8")
    return directory


def plan_mutation_lock_path(repo: Path, plan: str) -> Path:
    return plan_dir(repo, plan) / "state" / "mutation.lock"


@contextmanager
def plan_mutation_lock(repo: Path, plan: str):
    """Serialize every durable mutation for one plan.

    This is deliberately separate from the wake queue and tmux locks: those
    protect their own shared resources, while this lock protects the plan's
    board, run state, and lifecycle transitions as one transaction boundary.
    """
    key = str(plan_mutation_lock_path(repo, plan))
    held = getattr(_mutation_local, "held", {})
    if key in held:
        held[key][0] += 1
        try:
            yield
        finally:
            held[key][0] -= 1
        return
    path = Path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    held[key] = [1, handle]
    _mutation_local.held = held
    try:
        yield
    finally:
        held[key][0] -= 1
        if held[key][0] == 0:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            del held[key]


def execution_path(repo: Path, plan: str) -> Path:
    return plan_dir(repo, plan) / "state" / "execution.json"


def load_execution(repo: Path, plan: str) -> dict[str, object] | None:
    path = execution_path(repo, plan)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid execution record: {path}: {error.msg}") from error
    if not isinstance(record, dict) or not isinstance(record.get("run_id"), str) or not isinstance(record.get("tasks"), list):
        raise SystemExit(f"Invalid execution record: {path}")
    if not all(isinstance(task, str) for task in record["tasks"]):
        raise SystemExit(f"Invalid execution record: {path}")
    return record


def write_execution(repo: Path, plan: str, run_id: str, tasks: list[str]) -> None:
    write_atomic(
        execution_path(repo, plan),
        json.dumps({"version": 1, "run_id": run_id, "tasks": sorted(set(tasks)), "updated_at": utc_now()}, indent=2) + "\n",
    )


def active_in_progress_tasks(repo: Path, plan: str) -> list[Task]:
    return [task for task in read_tasks(repo, plan) if task.column == "in-progress"]


def validate_run_policy(mode: str | None, yolo: bool) -> dict[str, object]:
    if mode not in DELIVERY_MODES:
        raise SystemExit("invalid delivery mode; reserve requires --delivery-mode no-mistakes|direct-pr|local-only")
    return {"mode": mode, "yolo": yolo}


def policy_path(repo: Path, plan: str, run_id: str) -> Path:
    return run_dir(repo, plan, run_id) / "policy.json"


def append_progress(repo: Path, plan: str, run_id: str, task: Task, status: str) -> None:
    path = progress_path(repo, plan, run_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {task.path.name}: {status} (type {task.task_type})\n")


def move_task(repo: Path, plan: str, source_column: str, dest_column: str, task_name: str | None = None) -> Path:
    with plan_mutation_lock(repo, plan):
        base = plan_dir(repo, plan)
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
        rewrite_board(repo, plan)
        return target


def command_start(repo: Path, plan: str) -> None:
    tasks = read_tasks(repo, plan)
    completed = done_names(tasks)
    todo = sorted((task for task in tasks if task.column == "todo"), key=lambda task: task.path.name)
    startable = [task for task in todo if is_startable(task, completed)]
    if not startable:
        raise SystemExit("No startable todo task. Check Dependencies sections and done tasks.")

    selected = startable[0]
    parallels = parallel_candidates(todo, completed, selected)
    moved = move_task(repo, plan, "todo", "in-progress", selected.path.name)
    print(f"Started: {moved}")
    if parallels:
        print("Also startable in parallel:")
        for task in parallels:
            print(f"- {task.path.name}: {task.title}")


def command_plan(repo: Path, plan: str, limit: int) -> None:
    schedule = schedule_tasks(repo, plan, limit)
    print_schedule(schedule, limit)


def command_reserve(
    repo: Path, plan: str, limit: int, run_id: str, delivery_mode: str | None, yolo: bool
) -> None:
    policy = validate_run_policy(delivery_mode, yolo)
    with plan_mutation_lock(repo, plan):
        execution = load_execution(repo, plan)
        in_progress = active_in_progress_tasks(repo, plan)
        if in_progress and execution is None:
            raise SystemExit("in-progress work has no execution record; reserve explicitly before launching")
        if execution is not None and in_progress and execution["run_id"] != run_id:
            raise SystemExit(f"active execution run {execution['run_id']} owns in-progress work")
        if execution is not None and not in_progress:
            execution = None

        schedule = schedule_tasks(repo, plan, limit, run_id)
        reserved = [task.path.name for task in in_progress]
        reserved.extend(task.path.name for task in schedule.batch)
        if schedule.batch:
            ensure_run_dirs(repo, plan, run_id)
            write_atomic(policy_path(repo, plan, run_id), json.dumps(policy) + "\n")
            write_execution(repo, plan, run_id, reserved)
            for task in schedule.batch:
                source = plan_dir(repo, plan) / "todo" / task.path.name
                target = plan_dir(repo, plan) / "in-progress" / task.path.name
                shutil.move(str(source), str(target))
                append_progress(repo, plan, run_id, task, "in-progress")
            rewrite_board(repo, plan)

    print(f"Reserved launch batch (limit {limit}):")
    if not schedule.batch:
        print("- None")
        return
    for task in schedule.batch:
        moved = plan_dir(repo, plan) / "in-progress" / task.path.name
        print(f"- {task.path.name}: {task.title} -> {moved}")


def command_done(repo: Path, plan: str, task_name: str) -> None:
    moved = move_task(repo, plan, "in-progress", "done", task_name)
    print(f"Done: {moved}")


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SystemExit(detail or f"git {' '.join(args)} failed")
    return result.stdout


def resolved_commit(repo: Path, revision: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise SystemExit(f"unknown revision: {revision}")
    return result.stdout.strip()


def task_in_progress(repo: Path, plan: str, task_name: str) -> Task:
    matches = [
        task
        for task in read_tasks(repo, plan)
        if task.column == "in-progress" and task.path.name == task_name
    ]
    if not matches:
        raise SystemExit(f"Task is not in progress for plan {plan}: {task_name}")
    return matches[0]


def safe_relative_path(value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"{label} must be a relative path within the run directory")
    return path


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def tmux_plan_session_name(plan: str) -> str:
    return "-".join(("task-graph", re.sub(r"[^a-zA-Z0-9_-]+", "-", plan).strip("-")))


def tmux_window_name(run_id: str, task_name: str, role: str) -> str:
    parts = (run_id, Path(task_name).stem, role)
    return "-".join(re.sub(r"[^a-zA-Z0-9_-]+", "-", part).strip("-") for part in parts)


def tmux_target(record: dict[str, object]) -> str:
    return f"{record['session']}:{record['window']}"


def tmux_lock_path(repo: Path, plan: str) -> Path:
    return plan_dir(repo, plan) / "state" / "tmux.lock"


@contextmanager
def tmux_window_lock(repo: Path, plan: str):
    path = tmux_lock_path(repo, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def new_runtime_record(
    *, task: Task, plan: str, run_id: str, branch: str, worktree: Path, brief: Path,
    report: Path, log: Path, command: list[str], started_at: str | None = None,
    base_commit: str, role: str = "worker",
) -> dict[str, object]:
    return {
        "version": 2, "plan": plan, "run_id": run_id, "task": task.path.name,
        "session": tmux_plan_session_name(plan),
        "window": tmux_window_name(run_id, task.path.name, role), "role": role, "pid": None,
        "command": command, "branch": branch, "worktree": str(worktree),
        "base_commit": base_commit,
        "brief": str(brief), "report": str(report), "log": str(log),
        "launch_state": "intent", "launch_diagnostic": None,
        "started_at": started_at or utc_now(), "finished_at": None, "exit_code": None,
    }


def is_valid_runtime_record(record: object) -> bool:
    if not isinstance(record, dict) or not RUNTIME_FIELDS <= record.keys():
        return False
    return (
        record.get("version") == 2
        and isinstance(record.get("command"), list)
        and isinstance(record.get("task"), str)
        and isinstance(record.get("session"), str)
        and isinstance(record.get("window"), str)
        and isinstance(record.get("role"), str)
        and ("launch_state" not in record or record.get("launch_state") in LAUNCH_STATES)
    )


def runtime_launch_succeeded(record: dict[str, object]) -> bool:
    """Legacy runtime records predate launch_state and remain recoverable."""
    return record.get("launch_state", "launched") == "launched"


def record_launch_state(path: Path, record: dict[str, object], state: str, diagnostic: str | None = None) -> None:
    record["launch_state"] = state
    record["launch_diagnostic"] = diagnostic
    write_runtime_record(path, record)


def runtime_path(repo: Path, plan: str, run_id: str, task_name: str) -> Path:
    return run_dir(repo, plan, run_id) / "runtime" / f"{Path(task_name).stem}.json"


def write_runtime_record(path: Path, record: dict[str, object]) -> None:
    write_atomic(path, json.dumps(record, indent=2, sort_keys=True) + "\n")


def tmux_session_exists(session: str) -> bool:
    try:
        return subprocess.run(
            ["tmux", "has-session", "-t", session], text=True, capture_output=True
        ).returncode == 0
    except FileNotFoundError:
        return False


def tmux_target_exists(target: str) -> bool:
    """Check a named window target without mistaking a live session for that window."""
    if ":" not in target:
        return tmux_session_exists(target)
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_id}"],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except FileNotFoundError:
        return False


def tmux_liveness(session: str) -> str:
    if not tmux_target_exists(session):
        return "IDLE_OR_DEAD"
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session, "#{pane_current_command}"],
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return "UNKNOWN"
    if result.returncode:
        return "UNKNOWN"
    command = Path(result.stdout.strip()).name.lower()
    if command in {"codex", "claude", "grok", "opencode"}:
        return "RUNNING"
    if command == "bash":
        return "UNKNOWN"
    if command in {"zsh", "sh", "dash", "ash", "ksh", "fish"}:
        return "IDLE_OR_DEAD"
    return "UNKNOWN"


def tmux_create_window(
    repo: Path, plan: str, session: str, window: str, cwd: Path, command: str,
) -> int | None:
    """Create one activity window in a detached shared plan session."""
    with tmux_window_lock(repo, plan):
        target = f"{session}:{window}"
        if tmux_target_exists(target):
            raise TmuxTargetConflict(f"tmux target already exists: {target}")
        if tmux_session_exists(session):
            created = subprocess.run(
                ["tmux", "new-window", "-d", "-t", session, "-n", window, "-c", str(cwd), "bash", "-lc", command],
                text=True,
                capture_output=True,
            )
        else:
            created = subprocess.run(
                ["tmux", "new-session", "-d", "-s", session, "-n", window, "-c", str(cwd), "bash", "-lc", command],
                text=True,
                capture_output=True,
            )
        if created.returncode:
            detail = created.stderr.strip() or created.stdout.strip() or "tmux failed to create activity window"
            if "exist" in detail.lower() or "duplicate" in detail.lower():
                raise TmuxTargetConflict(f"tmux target conflict: {target}: {detail}")
            raise SystemExit(detail)
        subprocess.run(["tmux", "set-option", "-t", target, "remain-on-exit", "on"], capture_output=True)
        pane = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_pid}"], text=True, capture_output=True
        )
    return int(pane.stdout.strip()) if pane.returncode == 0 and pane.stdout.strip().isdigit() else None


def verified_worktree(repo: Path, worktree: Path, branch: str) -> tuple[Path, str]:
    root = worktree.resolve()
    if root == repo.resolve():
        raise SystemExit("--worktree must not be the controller checkout")
    top_level = Path(git_output(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if top_level != root:
        raise SystemExit("--worktree must be a Git worktree root")
    registered = {
        Path(line.removeprefix("worktree ")).resolve()
        for line in git_output(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }
    if root not in registered:
        raise SystemExit("--worktree must be a registered Git worktree")
    if git_output(root, "branch", "--show-current").strip() != branch:
        raise SystemExit("--worktree branch does not match --branch")
    return root, resolved_commit(root, "HEAD")


def command_launch_exec(
    repo: Path, plan: str, run_id: str, task_name: str, branch: str, worktree: Path, *, role: str = "worker",
) -> None:
    if not shutil.which("tmux"):
        raise SystemExit("tmux is required for launch-exec")
    with plan_mutation_lock(repo, plan):
        worktree, base_commit = verified_worktree(repo, worktree, branch)
        execution = load_execution(repo, plan)
        if execution is None or task_name not in execution["tasks"]:
            raise SystemExit(f"Task is not reserved in the active execution: {task_name}")
        if role == "worker" and execution["run_id"] != run_id:
            raise SystemExit(f"active execution run {execution['run_id']} does not match launch run {run_id}")
        task = task_in_progress(repo, plan, task_name)
        directory = ensure_run_dirs(repo, plan, run_id)
        brief = directory / "briefs" / task.path.name
        report = directory / "reports" / task.path.name
        log = directory / "logs" / f"{task.path.stem}.log"
        record_path = runtime_path(repo, plan, run_id, task.path.name)
        if record_path.exists():
            raise SystemExit(f"Runtime record already exists: {record_path}")
        if not brief.exists():
            raise SystemExit(f"Task brief does not exist: {brief}")

        command = [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            str(report),
        ]
        record = new_runtime_record(
            task=task, plan=plan, run_id=run_id, branch=branch, worktree=worktree,
            brief=brief, report=report, log=log, command=command, base_commit=base_commit, role=role,
        )
        target = tmux_target(record)
        if tmux_target_exists(target):
            raise SystemExit(f"tmux target already exists: {tmux_target(record)}")
        write_runtime_record(record_path, record)
        prompt = (
            f"Read {brief}. Work only on the assigned task in the current worktree. "
            "Do not write outside that worktree and do not run git commit. "
            "Return the complete final report in your final response, including status, summary, "
            "tests, concerns, and a suggested commit message."
        )
        codex_command = " ".join(shlex.quote(item) for item in [*command, prompt])
        wrapper = (
            f"set -o pipefail; {codex_command} 2>&1 | tee -a {shlex.quote(str(log))}; "
            "exit_code=${PIPESTATUS[0]}; "
            f"{shlex.quote(shutil.which('python3') or 'python3')} {shlex.quote(str(Path(__file__).resolve()))} "
            f"finish-runtime --repo {shlex.quote(str(repo))} --plan {shlex.quote(plan)} "
            f"--run-id {shlex.quote(run_id)} --task {shlex.quote(task.path.name)} --exit-code $exit_code; exit $exit_code"
        )
        session = str(record["session"])
        window = str(record["window"])
        try:
            pane_pid = tmux_create_window(repo, plan, session, window, worktree, wrapper)
        except TmuxTargetConflict as error:
            # The lock has been released.  An absent exact target now proves
            # this was a transient create race, so one retry is safe.
            if tmux_target_exists(target):
                record_launch_state(record_path, record, "conflicted", str(error))
                raise SystemExit(f"INSPECTION_REQUIRED: {error}") from error
            try:
                pane_pid = tmux_create_window(repo, plan, session, window, worktree, wrapper)
            except TmuxTargetConflict as retry_error:
                record_launch_state(record_path, record, "conflicted", str(retry_error))
                raise SystemExit(f"INSPECTION_REQUIRED: {retry_error}") from retry_error
            except SystemExit as retry_error:
                record_launch_state(record_path, record, "failed", str(retry_error))
                raise
        except SystemExit as error:
            record_launch_state(record_path, record, "failed", str(error))
            raise
        record_launch_state(record_path, record, "launched")
        if pane_pid is not None:
            record["pid"] = pane_pid
            write_runtime_record(record_path, record)
    print(f"Launched: {task.path.name}")
    print(f"Attach: tmux attach -t {session}")
    print(f"Log: {log}")


def command_finish_runtime(repo: Path, plan: str, run_id: str, task_name: str, exit_code: int) -> None:
    with plan_mutation_lock(repo, plan):
        path = runtime_path(repo, plan, run_id, task_name)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SystemExit(f"Runtime record does not exist: {path}")
        if not is_valid_runtime_record(record):
            raise SystemExit(f"Invalid runtime record: {path}")
        record["finished_at"] = utc_now()
        record["exit_code"] = exit_code
        write_runtime_record(path, record)


def resume_runtime_launch(repo: Path, plan: str, run_id: str, task_name: str) -> None:
    """Complete a crash-interrupted launch after its runtime intent was written."""
    path = runtime_path(repo, plan, run_id, task_name)
    record = load_json_object(path)
    if not is_valid_runtime_record(record):
        raise SystemExit("cannot resume invalid runtime launch")
    target = tmux_target(record)
    if runtime_launch_succeeded(record) and tmux_target_exists(target):
        return
    if tmux_target_exists(target):
        record_launch_state(path, record, "conflicted", f"tmux target already exists: {target}")
        raise SystemExit(f"INSPECTION_REQUIRED: tmux target already exists: {target}")
    worktree = Path(str(record["worktree"]))
    brief = Path(str(record["brief"]))
    log = Path(str(record["log"]))
    report = Path(str(record["report"]))
    command = [str(value) for value in record["command"]]
    prompt = (
        f"Read {brief}. Work only on the assigned task in the current worktree. "
        "Do not write outside that worktree and do not run git commit. "
        "Return the complete final report in your final response, including status, summary, tests, concerns, and a suggested commit message."
    )
    codex_command = " ".join(shlex.quote(item) for item in [*command, prompt])
    wrapper = (
        f"set -o pipefail; {codex_command} 2>&1 | tee -a {shlex.quote(str(log))}; "
        "exit_code=${PIPESTATUS[0]}; "
        f"{shlex.quote(shutil.which('python3') or 'python3')} {shlex.quote(str(Path(__file__).resolve()))} "
        f"finish-runtime --repo {shlex.quote(str(repo))} --plan {shlex.quote(plan)} --run-id {shlex.quote(run_id)} "
        f"--task {shlex.quote(task_name)} --exit-code $exit_code; exit $exit_code"
    )
    try:
        pane_pid = tmux_create_window(repo, plan, str(record["session"]), str(record["window"]), worktree, wrapper)
    except TmuxTargetConflict as error:
        if tmux_target_exists(target):
            record_launch_state(path, record, "conflicted", str(error))
            raise SystemExit(f"INSPECTION_REQUIRED: {error}") from error
        pane_pid = tmux_create_window(repo, plan, str(record["session"]), str(record["window"]), worktree, wrapper)
    record_launch_state(path, record, "launched")
    if pane_pid is not None:
        record["pid"] = pane_pid
        write_runtime_record(path, record)


def prepare_completed_task(repo: Path, plan: str, run_id: str, task_name: str) -> dict[str, object]:
    """Commit a successful worker worktree and return verified diff inputs.

    This intentionally does not integrate or clean up the task.  Those remain
    controller policy decisions after a separate review.
    """
    task_in_progress(repo, plan, task_name)
    try:
        record = json.loads(runtime_path(repo, plan, run_id, task_name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("prepare-completed-task requires a runtime record") from None
    if not is_valid_runtime_record(record) or record.get("exit_code") != 0:
        raise SystemExit("prepare-completed-task requires a successful valid runtime record")
    worktree = Path(str(record["worktree"]))
    if git_output(worktree, "status", "--porcelain").strip():
        staged = subprocess.run(["git", "add", "-A"], cwd=worktree, text=True, capture_output=True)
        if staged.returncode:
            raise SystemExit(staged.stderr.strip() or "git add failed")
        committed = subprocess.run(
            ["git", "commit", "-m", f"task-graph: complete {task_name}"],
            cwd=worktree,
            text=True,
            capture_output=True,
        )
        if committed.returncode:
            raise SystemExit(committed.stderr.strip() or "git commit failed")
    return {
        "record": record,
        "branch": str(record["branch"]),
        "base_commit": str(record["base_commit"]),
        "head_commit": resolved_commit(worktree, "HEAD"),
    }


def create_child_worktree(repo: Path, parent_branch: str, branch: str, worktree: Path) -> Path:
    """Create one controller-owned child worktree from a verified parent branch."""
    root = worktree.resolve()
    if root == repo.resolve() or root.exists():
        raise SystemExit("child worktree path must be new and distinct from the controller checkout")
    parent_head = resolved_commit(repo, parent_branch)
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(root), parent_head],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise SystemExit(result.stderr.strip() or "git worktree add failed")
    verified_worktree(repo, root, branch)
    return root


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def resolve_artifact(run: Path, value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    return path if path.is_absolute() else run / path


def final_report_status(report: Path | None) -> str | None:
    if not report or not report.exists():
        return None
    match = re.search(r"\b(DONE_WITH_CONCERNS|NEEDS_CONTEXT|BLOCKED|DONE)\b", report.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def review_status(review: Path) -> str | None:
    if not review.exists():
        return None
    match = re.search(r"Review status:\s*(approved|pending|changes_requested)\b", review.read_text(encoding="utf-8"), re.I)
    return match.group(1).lower() if match else None


def supervision_state_path(repo: Path, plan: str, task_name: str) -> Path:
    return plan_dir(repo, plan) / "state" / "tasks" / f"{Path(task_name).stem}.json"


def repair_attempts(repo: Path, plan: str, task_name: str) -> int:
    attempt = repair_attempt(repo, plan, task_name)
    if attempt is not None:
        return 1 if attempt.get("phase") == "launched" else 0
    return legacy_repair_attempts(repo, plan, task_name)


def legacy_repair_attempts(repo: Path, plan: str, task_name: str) -> int:
    path = supervision_state_path(repo, plan, task_name)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except json.JSONDecodeError:
        return 0
    return state.get("repair_attempts", 0) if isinstance(state.get("repair_attempts", 0), int) else 0


def repair_attempt(repo: Path, plan: str, task_name: str) -> dict[str, object] | None:
    path = supervision_state_path(repo, plan, task_name)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    attempt = state.get("repair_attempt")
    if not isinstance(attempt, dict):
        return None
    required = ("attempt", "child_run_id", "branch", "worktree", "phase")
    if not isinstance(attempt.get("attempt"), int) or any(not isinstance(attempt.get(key), str) for key in required[1:]):
        return None
    if attempt["phase"] not in {"reserved", "launched", "failed"}:
        return None
    return attempt


def write_repair_attempt_state(repo: Path, plan: str, task_name: str, attempt: dict[str, object]) -> None:
    phase = str(attempt["phase"])
    write_atomic(
        supervision_state_path(repo, plan, task_name),
        json.dumps(
            {
                "task": task_name,
                "repair_attempts": 1 if phase == "launched" else 0,
                "repair_attempt": attempt,
                "updated_at": utc_now(),
            },
            indent=2,
        )
        + "\n",
    )


def reserve_repair_attempt(
    repo: Path, plan: str, task_name: str, *, attempt: int, child_run_id: str, branch: str, worktree: Path
) -> dict[str, object]:
    record: dict[str, object] = {
        "attempt": attempt,
        "child_run_id": child_run_id,
        "branch": branch,
        "worktree": str(worktree),
        "phase": "reserved",
    }
    with plan_mutation_lock(repo, plan):
        write_repair_attempt_state(repo, plan, task_name, record)
    return record


def mark_repair_attempt_phase(repo: Path, plan: str, task_name: str, phase: str) -> dict[str, object]:
    if phase not in {"reserved", "launched", "failed"}:
        raise SystemExit(f"Invalid repair attempt phase: {phase}")
    with plan_mutation_lock(repo, plan):
        record = repair_attempt(repo, plan, task_name)
        if record is None:
            raise SystemExit(f"No repair attempt reserved for {task_name}")
        record["phase"] = phase
        write_repair_attempt_state(repo, plan, task_name, record)
        return record


def record_repair_attempt(repo: Path, plan: str, task_name: str, *, operation_id: str | None = None) -> None:
    path = supervision_state_path(repo, plan, task_name)
    existing = load_json_object(path)
    if operation_id is not None and existing.get("record_repair_operation_id") == operation_id:
        return
    attempts = legacy_repair_attempts(repo, plan, task_name) + 1
    write_atomic(
        path,
        json.dumps({"task": task_name, "repair_attempts": attempts, "record_repair_operation_id": operation_id, "updated_at": utc_now()}, indent=2) + "\n",
    )


def supervision_dir(repo: Path, plan: str) -> Path:
    return plan_dir(repo, plan) / "state"


def supervision_queue_path(repo: Path, plan: str) -> Path:
    return supervision_dir(repo, plan) / "wake-queue.jsonl"


def supervision_index_path(repo: Path, plan: str) -> Path:
    return supervision_dir(repo, plan) / "wake-index.json"


def wake_claims_path(repo: Path, plan: str) -> Path:
    return supervision_dir(repo, plan) / "wake-claims.json"


def wake_lock_path(repo: Path, plan: str) -> Path:
    return supervision_dir(repo, plan) / "wake-queue.lock"


@contextmanager
def wake_queue_lock(repo: Path, plan: str):
    """Serialize queue/index/claim mutations across watcher and controller processes."""
    path = wake_lock_path(repo, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_supervision_json_object(path: Path) -> dict[str, object]:
    """Read required coordination state without treating corruption as absence."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise SupervisionStateCorruption(path, error.msg) from error
    if not isinstance(value, dict):
        raise SupervisionStateCorruption(path, "expected a JSON object")
    return value


def append_jsonl_durable(path: Path, entries: list[dict[str, object]]) -> None:
    """Append JSONL records and confirm their contents are durable before returning."""
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = path.parent
    while True:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if directory.parent == directory:
            break
        directory = directory.parent


def _queued_wakes(repo: Path, plan: str) -> list[dict[str, object]]:
    path = supervision_queue_path(repo, plan)
    if not path.exists():
        return []
    wakes: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise SupervisionStateCorruption(path, "empty queue record", line_number)
        try:
            wake = json.loads(line)
        except json.JSONDecodeError as error:
            raise SupervisionStateCorruption(path, error.msg, line_number) from error
        if not isinstance(wake, dict) or any(
            not isinstance(wake.get(field), str) or not wake[field].strip()
            for field in ("id", "task", "run_id", "action")
        ):
            raise SupervisionStateCorruption(path, "missing required wake identity or action field", line_number)
        wakes.append(wake)
    return wakes


def queued_wakes(repo: Path, plan: str) -> list[dict[str, object]]:
    with wake_queue_lock(repo, plan):
        return _queued_wakes(repo, plan)


def repair_wake_queue(repo: Path, plan: str) -> dict[str, object]:
    """Snapshot and rebuild a corrupt wake queue without touching other state."""
    path = supervision_queue_path(repo, plan)
    with wake_queue_lock(repo, plan):
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        snapshot = path.with_name(f"{path.name}.before-repair-{uuid.uuid4().hex}.jsonl")
        write_atomic(snapshot, original)
        retained: list[dict[str, object]] = []
        discarded = 0
        for line in original.splitlines():
            try:
                wake = json.loads(line)
            except json.JSONDecodeError:
                discarded += 1
                continue
            if not isinstance(wake, dict) or any(
                not isinstance(wake.get(field), str) or not wake[field].strip()
                for field in ("id", "task", "run_id", "action")
            ):
                discarded += 1
                continue
            retained.append(wake)
        write_atomic(path, "".join(json.dumps(wake, sort_keys=True) + "\n" for wake in retained))
    return {"snapshot": str(snapshot), "retained": len(retained), "discarded": discarded}


def claim_wake(repo: Path, plan: str, wake_id: str) -> dict[str, object]:
    with wake_queue_lock(repo, plan):
        wakes = {str(wake["id"]): wake for wake in _queued_wakes(repo, plan) if "id" in wake}
        if wake_id not in wakes:
            raise SystemExit(f"Unknown wake: {wake_id}")
        claims = load_supervision_json_object(wake_claims_path(repo, plan))
        if claims.get(wake_id) in {"acknowledged", "escalated"}:
            raise SystemExit(f"Wake already {claims[wake_id]}: {wake_id}")
        if claims.get(wake_id) == "claimed":
            raise SystemExit(f"Wake already claimed: {wake_id}")
        claims[wake_id] = "claimed"
        write_atomic(wake_claims_path(repo, plan), json.dumps(claims, indent=2, sort_keys=True) + "\n")
        return wakes[wake_id]


def acknowledge_wake(repo: Path, plan: str, wake_id: str) -> None:
    with wake_queue_lock(repo, plan):
        claims = load_supervision_json_object(wake_claims_path(repo, plan))
        if claims.get(wake_id) != "claimed":
            raise SystemExit(f"Wake must be claimed before acknowledgement: {wake_id}")
        claims[wake_id] = "acknowledged"
        write_atomic(wake_claims_path(repo, plan), json.dumps(claims, indent=2, sort_keys=True) + "\n")


def escalate_wake(repo: Path, plan: str, wake_id: str) -> None:
    with wake_queue_lock(repo, plan):
        claims = load_supervision_json_object(wake_claims_path(repo, plan))
        if claims.get(wake_id) != "claimed":
            raise SystemExit(f"Wake must be claimed before escalation: {wake_id}")
        claims[wake_id] = "escalated"
        write_atomic(wake_claims_path(repo, plan), json.dumps(claims, indent=2, sort_keys=True) + "\n")


def supervise_once(repo: Path, plan: str) -> list[dict[str, object]]:
    with plan_mutation_lock(repo, plan):
        return _supervise_once(repo, plan)


def _supervise_once(repo: Path, plan: str) -> list[dict[str, object]]:
    """Persist newly actionable controller work before returning it to a watcher."""
    actions = reconcile_actions(repo, plan)
    with wake_queue_lock(repo, plan):
        index_path = supervision_index_path(repo, plan)
        seen = load_supervision_json_object(index_path)
        queued_ids = {str(wake.get("id")) for wake in _queued_wakes(repo, plan) if wake.get("id")}
        new_wakes: list[dict[str, object]] = []
        durable_fingerprints: dict[str, str] = {}
        for action in actions:
            fingerprint = f"{action['task']}:{action.get('run_id', '-') }:{action['action']}"
            wake_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
            if wake_id not in queued_ids:
                new_wakes.append({**action, "id": wake_id})
            durable_fingerprints[str(action["task"])] = fingerprint
        if new_wakes:
            append_jsonl_durable(supervision_queue_path(repo, plan), new_wakes)
        seen.update(durable_fingerprints)
        write_atomic(index_path, json.dumps(seen, indent=2, sort_keys=True) + "\n")
        write_atomic(supervision_dir(repo, plan) / "watcher.json", json.dumps({"updated_at": utc_now()}) + "\n")
        return new_wakes


def command_supervise(repo: Path, plan: str, seconds: int) -> int:
    if seconds <= 0:
        raise SystemExit("--seconds must be greater than zero")
    started = time.monotonic()
    while True:
        wakes = supervise_once(repo, plan)
        if wakes:
            print("signal: controller action required")
            for wake in wakes:
                print(f"{wake['task']}: {wake['action']}")
            return 0
        remaining = seconds - (time.monotonic() - started)
        if remaining <= 0:
            print(f"checkpoint: no actionable wake within {seconds}s")
            return 124
        time.sleep(min(EXEC_WATCH_INTERVAL_SECONDS, remaining))


def command_stop_guard(repo: Path, plan: str) -> int:
    """Codex Stop-hook predicate; block once when actionable work remains."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if payload.get("stop_hook_active") is True:
        return 0
    actions = reconcile_actions(repo, plan)
    if not actions:
        return 0
    print("TURN WOULD END BLIND: drain Task Graph reconciliation actions before stopping.", file=sys.stderr)
    for action in actions:
        print(f"- {action['task']}: {action['action']}", file=sys.stderr)
    return 2


def command_install_stop_hook(repo: Path, plan: str) -> None:
    """Merge a scoped Stop hook into a target repository without replacing hooks."""
    config_path = repo / ".codex" / "hooks.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {"hooks": {}}
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid Codex hooks file: {config_path}: {error}") from None
    hooks = config.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    command = stop_hook_command(repo, plan)
    if not any(command in str(item) for item in stop):
        stop.append({"hooks": [{"type": "command", "command": command, "timeout": 30}]})
    write_atomic(config_path, json.dumps(config, indent=2) + "\n")
    print(f"Installed Task Graph Stop hook: {config_path}")


def command_uninstall_stop_hook(repo: Path, plan: str) -> None:
    config_path = repo / ".codex" / "hooks.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Task Graph Stop hook is not installed: {config_path}")
        return
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid Codex hooks file: {config_path}: {error}") from None
    stop = config.get("hooks", {}).get("Stop", [])
    if not isinstance(stop, list):
        raise SystemExit(f"Invalid Codex Stop hooks: {config_path}")
    retained = [item for item in stop if "kanban.py stop-guard" not in json.dumps(item, sort_keys=True)]
    config["hooks"]["Stop"] = retained
    write_atomic(config_path, json.dumps(config, indent=2) + "\n")
    print(f"Uninstalled Task Graph Stop hook: {config_path}")


def stop_hook_command(repo: Path, plan: str) -> str:
    return " ".join(
        shlex.quote(value)
        for value in ("python3", str(Path(__file__).resolve()), "stop-guard", "--repo", str(repo), "--plan", plan)
    )


def stop_hook_installed(repo: Path, plan: str) -> bool:
    try:
        config = json.loads((repo / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    stop = config.get("hooks", {}).get("Stop", []) if isinstance(config, dict) else []
    return isinstance(stop, list) and any(stop_hook_command(repo, plan) in str(item) for item in stop)


def latest_runtime_record(repo: Path, plan: str, task_name: str) -> tuple[str, Path, dict[str, object]] | None:
    candidates: list[tuple[str, Path, dict[str, object]]] = []
    runs = plan_dir(repo, plan) / "runs"
    if not runs.exists():
        return None
    for run in runs.iterdir():
        path = run / "runtime" / f"{Path(task_name).stem}.json"
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if is_valid_runtime_record(record) and record.get("task") == task_name:
            candidates.append((str(record.get("finished_at") or record.get("started_at") or ""), run, record))
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1].name), default=None)


def runtime_record_for_run(repo: Path, plan: str, run_id: str, task_name: str) -> tuple[str, Path, dict[str, object]] | None:
    run = plan_dir(repo, plan) / "runs" / run_id
    path = run / "runtime" / f"{Path(task_name).stem}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not is_valid_runtime_record(record) or record.get("task") != task_name:
        return None
    return run_id, run, record


def reconcile_actions(repo: Path, plan: str) -> list[dict[str, object]]:
    """Return each active task's single required controller action.

    Runtime files are evidence only; the board's in-progress column selects live tasks.
    """
    actions: list[dict[str, object]] = []
    for task in read_tasks_readonly(repo, plan):
        if task.column != "in-progress":
            continue
        current = latest_runtime_record(repo, plan, task.path.name)
        if current is None:
            actions.append({"task": task.path.name, "action": "INSPECTION_REQUIRED", "reason": "No active runtime record."})
            continue
        _, run, record = current
        health, _ = runtime_health(
            run, task.path.name, record, tmux_alive=tmux_liveness,
            stale_after=timedelta(minutes=30), now=datetime.now(UTC),
        )
        if health == "RUNNING":
            continue
        report = resolve_artifact(run, record.get("report"))
        result = final_report_status(report)
        review = run / "reviews" / task.path.name
        verdict = review_status(review)
        if result == "DONE" and record.get("exit_code") == 0:
            if verdict in {None, "pending"}:
                action, reason = "REVIEW_REQUIRED", "Completed worker has no approved review."
            elif verdict == "changes_requested":
                if repair_attempts(repo, plan, task.path.name) < 1:
                    action, reason = "REPAIR_REQUIRED", "Review requested changes."
                else:
                    action, reason = "RETRY_DECISION_REQUIRED", "Focused repair already attempted."
            else:
                action, reason = "DELIVERY_REQUIRED", "Review approved; await delivery policy."
        elif result == "DONE_WITH_CONCERNS":
            if repair_attempts(repo, plan, task.path.name) < 1:
                action, reason = "REPAIR_REQUIRED", "Worker reported concerns."
            else:
                action, reason = "RETRY_DECISION_REQUIRED", "Focused repair already attempted."
        elif result in {"NEEDS_CONTEXT", "BLOCKED"}:
            action, reason = "USER_CONTEXT_REQUIRED", f"Worker reported {result}."
        else:
            action, reason = "INSPECTION_REQUIRED", "Runtime is incomplete, failed, stale, or lacks a valid final report."
        actions.append({"task": task.path.name, "run_id": run.name, "action": action, "reason": reason})
    return actions


def read_run_policy(repo: Path, plan: str, run_id: str) -> dict[str, object]:
    path = policy_path(repo, plan, run_id)
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Missing run policy: {path}") from None
    if not isinstance(policy, dict) or not isinstance(policy.get("yolo"), bool):
        raise SystemExit(f"Invalid run policy: {path}")
    return validate_run_policy(policy.get("mode"), policy["yolo"])


def command_delivery_ready(repo: Path, plan: str, run_id: str, task_name: str) -> str:
    policy = read_run_policy(repo, plan, run_id)
    run = run_dir(repo, plan, run_id)
    try:
        record = json.loads(runtime_path(repo, plan, run_id, task_name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("delivery requires verified review and tests") from None
    report = resolve_artifact(run, record.get("report")) if isinstance(record, dict) else None
    review = run / "reviews" / task_name
    if (
        not is_valid_runtime_record(record)
        or record.get("exit_code") != 0
        or final_report_status(report) != "DONE"
        or not review.exists()
        or "Review status: approved" not in review.read_text(encoding="utf-8")
    ):
        raise SystemExit("delivery requires verified review and tests")
    mode = policy["mode"]
    yolo = policy["yolo"]
    if mode == "no-mistakes":
        return "RUN_NO_MISTAKES"
    if mode == "direct-pr":
        return "MERGE_GREEN_PR" if yolo else "OPEN_PR"
    return "FAST_FORWARD_LOCAL" if yolo else "AWAIT_LOCAL_APPROVAL"


def delivery_path(repo: Path, plan: str, run_id: str, task_name: str) -> Path:
    return run_dir(repo, plan, run_id) / "delivery" / f"{Path(task_name).stem}.json"


def teardown_path(repo: Path, plan: str, run_id: str, task_name: str) -> Path:
    return run_dir(repo, plan, run_id) / "teardown" / f"{Path(task_name).stem}.json"


def write_teardown_progress(repo: Path, plan: str, run_id: str, task_name: str, **updates: object) -> dict[str, object]:
    path = teardown_path(repo, plan, run_id, task_name)
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        progress = {"task": task_name, "worktree_removed": False, "window_removed": False, "completed": False}
    if not isinstance(progress, dict):
        raise SystemExit(f"Invalid teardown progress: {path}")
    progress.update(updates)
    progress["updated_at"] = utc_now()
    write_atomic(path, json.dumps(progress, indent=2, sort_keys=True) + "\n")
    return progress


def command_record_delivery(repo: Path, plan: str, run_id: str, task_name: str, result: str) -> None:
    with plan_mutation_lock(repo, plan):
        _command_record_delivery(repo, plan, run_id, task_name, result)


def _command_record_delivery(repo: Path, plan: str, run_id: str, task_name: str, result: str) -> None:
    if result != "landed":
        raise SystemExit("record-delivery requires --result landed")
    try:
        record = json.loads(runtime_path(repo, plan, run_id, task_name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("record-delivery requires a runtime record") from None
    if not is_valid_runtime_record(record):
        raise SystemExit("record-delivery requires a valid runtime record")
    write_atomic(delivery_path(repo, plan, run_id, task_name), json.dumps({"result": result, "at": utc_now()}) + "\n")


def command_teardown(repo: Path, plan: str, run_id: str, task_name: str, discard: bool) -> None:
    with plan_mutation_lock(repo, plan):
        _command_teardown(repo, plan, run_id, task_name, discard)


def _command_teardown(repo: Path, plan: str, run_id: str, task_name: str, discard: bool) -> None:
    try:
        prior = json.loads(teardown_path(repo, plan, run_id, task_name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        prior = {}
    if isinstance(prior, dict) and prior.get("completed") is True:
        return
    try:
        record = json.loads(runtime_path(repo, plan, run_id, task_name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("teardown requires a runtime record") from None
    if not is_valid_runtime_record(record):
        raise SystemExit("teardown requires a valid runtime record")
    delivery = delivery_path(repo, plan, run_id, task_name)
    landed = delivery.exists() and json.loads(delivery.read_text(encoding="utf-8")).get("result") == "landed"
    if not landed and not discard:
        raise SystemExit("teardown refuses dirty or unlanded work without explicit discard")
    worktree = Path(str(record["worktree"]))
    dirty = bool(git_output(worktree, "status", "--porcelain").strip())
    if dirty and not discard:
        raise SystemExit("teardown refuses dirty or unlanded work without explicit discard")
    if discard:
        write_atomic(delivery, json.dumps({"result": "discarded", "at": utc_now()}) + "\n")
    progress = write_teardown_progress(repo, plan, run_id, task_name, started_at=utc_now())
    if not progress.get("worktree_removed"):
        result = subprocess.run(["git", "worktree", "remove", str(worktree)], cwd=repo, text=True, capture_output=True)
        if result.returncode and worktree.exists():
            raise SystemExit(result.stderr.strip() or "git worktree remove failed")
        write_teardown_progress(repo, plan, run_id, task_name, worktree_removed=True)
    target = tmux_target(record)
    progress = json.loads(teardown_path(repo, plan, run_id, task_name).read_text(encoding="utf-8"))
    if not progress.get("window_removed"):
        with tmux_window_lock(repo, plan):
            tmux = subprocess.run(
                ["tmux", "kill-window", "-t", target], cwd=repo, text=True, capture_output=True,
            )
        if tmux.returncode and "can't find session" not in tmux.stderr.lower():
            raise SystemExit(tmux.stderr.strip() or tmux.stdout.strip() or "tmux failed to remove session")
        write_teardown_progress(repo, plan, run_id, task_name, window_removed=True)
    write_teardown_progress(repo, plan, run_id, task_name, completed=True, completed_at=utc_now())


def runtime_activity_at(run: Path, task_name: str, record: dict[str, object]) -> datetime | None:
    """Return the most recent durable worker artifact update."""
    report = resolve_artifact(run, record["report"])
    log = resolve_artifact(run, record["log"])
    runtime = run / "runtime" / f"{Path(task_name).stem}.json"
    activities = [path.stat().st_mtime for path in (report, log, runtime) if path and path.exists()]
    return datetime.fromtimestamp(max(activities), UTC) if activities else None


def runtime_health(
    run: Path, task_name: str, record: dict[str, object], *, tmux_alive: callable,
    stale_after: timedelta, now: datetime,
) -> tuple[str, datetime | None]:
    """Classify active worker health from tmux liveness and durable activity."""
    activity = runtime_activity_at(run, task_name, record)
    liveness = tmux_alive(tmux_target(record))
    if liveness == "RUNNING":
        if activity is not None and now - activity <= stale_after:
            return "RUNNING", activity
        return "STALE", activity
    if liveness == "UNKNOWN":
        return "UNKNOWN", activity
    if activity is not None and now - activity > stale_after:
        return "STALE", activity
    return "UNKNOWN", activity


def status_entry(
    *, plan: str, run_id: str, task_name: str, run: Path, record: dict[str, object] | None,
    tmux_alive: callable, stale_after: timedelta, now: datetime,
) -> dict[str, object]:
    if record is None or not is_valid_runtime_record(record):
        return {"plan": plan, "run_id": run_id, "task": task_name, "state": "UNKNOWN", "elapsed": None,
                "session": None, "report_status": None, "last_activity": None,
                "recovery_hint": "Inspect the legacy run ledger and task artifacts before relaunching manually."}
    report = resolve_artifact(run, record["report"])
    log = resolve_artifact(run, record["log"])
    report_status = final_report_status(report)
    started = parse_timestamp(record["started_at"])
    finished = parse_timestamp(record["finished_at"])
    elapsed_seconds = int(((finished or now) - started).total_seconds()) if started else None
    health, activity = runtime_health(
        run, task_name, record, tmux_alive=tmux_alive, stale_after=stale_after, now=now,
    )
    last_activity = activity.isoformat() if activity else None
    if health == "RUNNING":
        state, hint = "RUNNING", f"Attach: tmux attach -t {record['session']}"
    elif record["exit_code"] == 0 and report_status == "DONE":
        state, hint = "SUCCEEDED_AWAITING_REVIEW", f"Open report: {report}"
    elif report_status == "DONE_WITH_CONCERNS":
        state, hint = "NEEDS_ATTENTION", f"Read report: {report}; launch the one focused repair attempt."
    elif record["exit_code"] not in (None, 0) or report_status in {"NEEDS_CONTEXT", "BLOCKED"}:
        state, hint = "NEEDS_ATTENTION", f"Inspect log: {log}; investigate or relaunch manually."
    elif health == "STALE":
        state, hint = "STALE", f"Inspect log: {log}; investigate or relaunch manually."
    elif health == "UNKNOWN":
        state, hint = "UNKNOWN", f"Inspect tmux pane and runtime record: {log}"
    else:
        state, hint = "UNKNOWN", f"Inspect runtime record and log: {log}"
    return {"plan": plan, "run_id": run_id, "task": task_name, "state": state,
            "elapsed": elapsed_seconds, "session": record["session"], "window": record.get("window"), "report_status": report_status,
            "last_activity": last_activity,
            "recovery_hint": hint}


def collect_status(
    repo: Path, plan: str | None = None, run_id: str | None = None, task_name: str | None = None,
    *, tmux_alive: callable = tmux_liveness, stale_after: timedelta = timedelta(minutes=30),
) -> list[dict[str, object]]:
    root = repo / ".agent"
    if not root.exists():
        return []
    plans = [plan_dir(repo, plan)] if plan else sorted(path for path in root.iterdir() if path.is_dir())
    now = datetime.now(UTC)
    entries: list[dict[str, object]] = []
    for current_plan in plans:
        runs = current_plan / "runs"
        if not runs.exists():
            continue
        tasks = {
            task.path.name
            for task in read_tasks_readonly(repo, current_plan.name)
            if task.column == "in-progress"
        }
        for current_run in sorted(path for path in runs.iterdir() if path.is_dir()):
            if run_id and current_run.name != run_id:
                continue
            records: dict[str, dict[str, object] | None] = {}
            for path in sorted((current_run / "runtime").glob("*.json")) if (current_run / "runtime").exists() else []:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    records[str(record.get("task", path.with_suffix(".md").name))] = record
                except json.JSONDecodeError:
                    records[path.with_suffix(".md").name] = None
            # Completed task runtime records are archival history, not active work.
            names = tasks
            for name in sorted(names):
                if task_name and name != task_name:
                    continue
                entries.append(status_entry(plan=current_plan.name, run_id=current_run.name, task_name=name,
                                            run=current_run, record=records.get(name), tmux_alive=tmux_alive,
                                            stale_after=stale_after, now=now))
    return entries


def format_elapsed(seconds: object) -> str:
    if not isinstance(seconds, int):
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def print_status(entries: list[dict[str, object]]) -> None:
    headers = ("PLAN", "RUN", "TASK", "STATE", "REPORT", "ELAPSED", "TMUX", "LAST ACTIVITY", "NEXT STEP")
    rows = [headers] + [(
        str(entry["plan"]), str(entry["run_id"]), str(entry["task"]), str(entry["state"]),
        str(entry.get("report_status") or "-"), format_elapsed(entry["elapsed"]),
        str(entry["session"] or "-"), str(entry["last_activity"] or "-"),
        str(entry["recovery_hint"]),
    ) for entry in entries]
    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    for index, row in enumerate(rows):
        print("  ".join(value.ljust(widths[column]) for column, value in enumerate(row)))
        if index == 0:
            print("  ".join("-" * width for width in widths))
    if not entries:
        print("No active task executions found.")


def truncate_text(value: object, width: int) -> str:
    text = str(value)
    return text if len(text) <= width else f"{text[:width - 1]}…"


def command_status(repo: Path, plan: str | None, run_id: str | None, task_name: str | None, as_json: bool) -> None:
    entries = collect_status(repo, plan, run_id, task_name)
    if as_json:
        print(json.dumps({"tasks": entries}, indent=2, sort_keys=True))
    else:
        print_status(entries)


def command_archive_diff(
    repo: Path,
    plan: str,
    run_id: str,
    task_name: str,
    base: str,
    head: str,
    branch: str,
    review: str,
) -> None:
    task = task_in_progress(repo, plan, task_name)
    base_commit = resolved_commit(repo, base)
    head_commit = resolved_commit(repo, head)
    review_path = safe_relative_path(review, "--review")
    patch = git_output(repo, "diff", "--binary", "--full-index", f"{base_commit}..{head_commit}")
    stat = git_output(repo, "diff", "--stat", f"{base_commit}..{head_commit}").rstrip()

    directory = ensure_run_dirs(repo, plan, run_id) / "diffs"
    patch_path = directory / f"{task.path.stem}.patch"
    summary_path = directory / f"{task.path.stem}.md"
    summary = "\n".join(
        [
            f"# Diff Package: {task.path.name}",
            "",
            f"- Task: `{task.path.name}`",
            f"- Branch: `{branch}`",
            f"- Base commit: `{base_commit}`",
            f"- Head commit: `{head_commit}`",
            f"- Review: `{review_path.as_posix()}`",
            "- Review status: `pending`",
            f"- Patch: `diffs/{patch_path.name}`",
            "",
            "## Changed Files",
            "",
            "```text",
            stat or "No changed files.",
            "```",
            "",
        ]
    )
    write_atomic(patch_path, patch)
    write_atomic(summary_path, summary)
    print(f"Patch: {patch_path}")
    print(f"Summary: {summary_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("archive-diff", "board", "plan", "reserve", "start", "done", "launch-exec", "finish-runtime", "delivery-ready", "record-delivery", "teardown", "status", "watch-exec", "reconcile", "record-repair", "supervise", "stop-guard", "install-stop-hook", "uninstall-stop-hook", "claim-wake", "ack-wake"),
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--plan", help="Lowercase kebab-case plan slug")
    parser.add_argument("--task", help="Task filename for the done command")
    parser.add_argument("--base", help="Base commit for archive-diff")
    parser.add_argument("--head", help="Head commit for archive-diff")
    parser.add_argument("--branch", help="Task branch for archive-diff")
    parser.add_argument("--review", help="Relative review path for archive-diff")
    parser.add_argument("--limit", type=int, default=5, help="Maximum recommended parallel launch count")
    parser.add_argument("--run-id", help="Run identifier for run ledger commands")
    parser.add_argument("--delivery-mode", choices=sorted(DELIVERY_MODES))
    parser.add_argument("--yolo", action="store_true", help="Allow green routine delivery for this run")
    parser.add_argument("--discard", action="store_true", help="Explicitly discard unlanded worker work")
    parser.add_argument("--result", help="Recorded delivery result")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON where supported")
    parser.add_argument("--worktree", type=Path, help="Dedicated task worktree for launch-exec")
    parser.add_argument("--exit-code", type=int, help="Wrapper exit code for finish-runtime")
    parser.add_argument("--watch", action="store_true", help="Continuously redraw status")
    parser.add_argument("--checkpoint", action="store_true", help="Exit watch-exec when controller attention is needed")
    parser.add_argument("--interval", type=float, default=2.0, help="Status refresh interval in seconds")
    parser.add_argument("--seconds", type=int, help="Maximum watch-exec monitor duration in seconds")
    parser.add_argument("--wake-id", help="Durable wake identifier")
    parser.add_argument("--operation-id", help="Stable id for a controller-owned mutation request")
    args = parser.parse_args()

    repo = args.repo.resolve()
    if args.command == "status":
        if args.watch:
            import watcher

            watcher.watch_status(repo, args.plan, args.run_id, args.task, args.interval, as_json=args.json)
        else:
            command_status(repo, args.plan, args.run_id, args.task, args.json)
        return
    if args.command == "watch-exec":
        import watcher

        if args.seconds is None:
            raise SystemExit("watch-exec requires --seconds <positive-int>")
        raise SystemExit(watcher.watch_exec(repo, args.plan, args.run_id, args.task, args.seconds, checkpoint=args.checkpoint))
    if not args.plan:
        raise SystemExit(f"{args.command} requires --plan <plan-slug>")
    # Validate syntax before the controller-client protocol so malformed plan
    # names retain the CLI's precise error instead of looking like migration.
    plan_dir(repo, args.plan)
    mutation_commands = {"board", "reserve", "start", "done", "record-repair", "record-delivery", "teardown", "launch-exec", "archive-diff", "finish-runtime", "supervise", "claim-wake", "ack-wake", "install-stop-hook", "uninstall-stop-hook"}
    if args.command in mutation_commands:
        values: dict[str, object] = {
            "plan": args.plan, "limit": args.limit, "run_id": args.run_id, "task": args.task,
            "delivery_mode": args.delivery_mode, "yolo": args.yolo, "result": args.result,
            "discard": args.discard, "branch": args.branch, "worktree": str(args.worktree) if args.worktree else None,
            "base": args.base, "head": args.head, "review": args.review,
            "exit_code": args.exit_code, "seconds": args.seconds, "wake_id": args.wake_id,
        }
        operation_id = args.operation_id or new_operation_id()
        print(json.dumps(submit_controller_request(repo, args.plan, args.command, values, operation_id=operation_id), sort_keys=True))
        return
    if args.command == "stop-guard":
        raise SystemExit(command_stop_guard(repo, args.plan))
    if args.command == "install-stop-hook":
        command_install_stop_hook(repo, args.plan)
        return
    if args.command == "uninstall-stop-hook":
        command_uninstall_stop_hook(repo, args.plan)
        return
    if args.command == "claim-wake":
        if not args.wake_id:
            raise SystemExit("claim-wake requires --wake-id <id>")
        print(json.dumps(claim_wake(repo, args.plan, args.wake_id), sort_keys=True))
        return
    if args.command == "ack-wake":
        if not args.wake_id:
            raise SystemExit("ack-wake requires --wake-id <id>")
        acknowledge_wake(repo, args.plan, args.wake_id)
        print(f"Acknowledged wake: {args.wake_id}")
        return
    if args.command == "supervise":
        if args.seconds is None:
            raise SystemExit("supervise requires --seconds <positive-int>")
        raise SystemExit(command_supervise(repo, args.plan, args.seconds))
    if args.command == "board":
        print(f"Board: {rewrite_board(repo, args.plan)}")
    elif args.command == "plan":
        schedule = schedule_tasks(repo, args.plan, args.limit)
        if args.json:
            print_schedule_json(schedule, args.limit)
        else:
            print_schedule(schedule, args.limit)
    elif args.command == "reconcile":
        actions = reconcile_actions(repo, args.plan)
        if args.json:
            print(json.dumps({"actions": actions}, indent=2, sort_keys=True))
        elif actions:
            for action in actions:
                print(f"{action['task']}: {action['action']} - {action['reason']}")
        else:
            print("No autonomous controller action required.")
    elif args.command == "record-repair":
        if not args.task:
            raise SystemExit("record-repair requires --task <filename>")
        task_in_progress(repo, args.plan, args.task)
        record_repair_attempt(repo, args.plan, args.task)
        print(f"Recorded repair attempt: {args.task}")
    elif args.command == "reserve":
        if not args.run_id:
            raise SystemExit("reserve requires --run-id <id>")
        command_reserve(repo, args.plan, args.limit, args.run_id, args.delivery_mode, args.yolo)
    elif args.command == "start":
        command_start(repo, args.plan)
    elif args.command == "done":
        if not args.task:
            raise SystemExit("done requires --task <filename>")
        command_done(repo, args.plan, args.task)
    elif args.command == "archive-diff":
        required = {
            "--run-id": args.run_id,
            "--task": args.task,
            "--base": args.base,
            "--head": args.head,
            "--branch": args.branch,
            "--review": args.review,
        }
        missing = [flag for flag, value in required.items() if not value]
        if missing:
            raise SystemExit(f"archive-diff requires {' '.join(missing)}")
        command_archive_diff(
            repo,
            args.plan,
            args.run_id,
            args.task,
            args.base,
            args.head,
            args.branch,
            args.review,
        )
    elif args.command == "launch-exec":
        required = {"--run-id": args.run_id, "--task": args.task, "--branch": args.branch, "--worktree": args.worktree}
        missing = [flag for flag, value in required.items() if not value]
        if missing:
            raise SystemExit(f"launch-exec requires {' '.join(missing)}")
        command_launch_exec(repo, args.plan, args.run_id, args.task, args.branch, args.worktree.resolve())
    elif args.command == "finish-runtime":
        required = {"--run-id": args.run_id, "--task": args.task, "--exit-code": args.exit_code}
        missing = [flag for flag, value in required.items() if value is None or value == ""]
        if missing:
            raise SystemExit(f"finish-runtime requires {' '.join(missing)}")
        command_finish_runtime(repo, args.plan, args.run_id, args.task, args.exit_code)
    elif args.command == "delivery-ready":
        if not args.run_id or not args.task:
            raise SystemExit("delivery-ready requires --run-id <id> --task <filename>")
        print(command_delivery_ready(repo, args.plan, args.run_id, args.task))
    elif args.command == "teardown":
        if not args.run_id or not args.task:
            raise SystemExit("teardown requires --run-id <id> --task <filename>")
        command_teardown(repo, args.plan, args.run_id, args.task, args.discard)
    elif args.command == "record-delivery":
        if not args.run_id or not args.task or not args.result:
            raise SystemExit("record-delivery requires --run-id <id> --task <filename> --result landed")
        command_record_delivery(repo, args.plan, args.run_id, args.task, args.result)


if __name__ == "__main__":
    main()
