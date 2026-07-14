#!/usr/bin/env python3
"""Manage project-local .agent kanban task files."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


COLUMNS = ("todo", "in-progress", "done")
DELIVERY_MODES = frozenset({"no-mistakes", "direct-pr", "local-only"})
PLAN_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
RUNTIME_FIELDS = {
    "version", "plan", "run_id", "task", "session", "pid", "command", "branch",
    "worktree", "base_commit", "brief", "report", "log", "started_at", "finished_at", "exit_code",
}


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
    ensure_run_dirs(repo, plan, run_id)
    write_atomic(policy_path(repo, plan, run_id), json.dumps(policy) + "\n")
    schedule = schedule_tasks(repo, plan, limit, run_id)

    print(f"Reserved launch batch (limit {limit}):")
    if not schedule.batch:
        print("- None")
        return

    for task in schedule.batch:
        moved = move_task(repo, plan, "todo", "in-progress", task.path.name)
        append_progress(repo, plan, run_id, task, "in-progress")
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
        temporary = Path(handle.name)
    temporary.replace(path)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def tmux_session_name(plan: str, run_id: str, task_name: str) -> str:
    parts = ("task-graph", plan, run_id, Path(task_name).stem)
    return "-".join(re.sub(r"[^a-zA-Z0-9_-]+", "-", part).strip("-") for part in parts)


def new_runtime_record(
    *, task: Task, plan: str, run_id: str, branch: str, worktree: Path, brief: Path,
    report: Path, log: Path, command: list[str], started_at: str | None = None,
    base_commit: str,
) -> dict[str, object]:
    return {
        "version": 1, "plan": plan, "run_id": run_id, "task": task.path.name,
        "session": tmux_session_name(plan, run_id, task.path.name), "pid": None,
        "command": command, "branch": branch, "worktree": str(worktree),
        "base_commit": base_commit,
        "brief": str(brief), "report": str(report), "log": str(log),
        "started_at": started_at or utc_now(), "finished_at": None, "exit_code": None,
    }


def is_valid_runtime_record(record: object) -> bool:
    if not isinstance(record, dict) or not RUNTIME_FIELDS <= record.keys():
        return False
    return (
        record.get("version") == 1
        and isinstance(record.get("command"), list)
        and isinstance(record.get("task"), str)
        and isinstance(record.get("session"), str)
    )


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
    repo: Path, plan: str, run_id: str, task_name: str, branch: str, worktree: Path
) -> None:
    if not shutil.which("tmux"):
        raise SystemExit("tmux is required for launch-exec")
    worktree, base_commit = verified_worktree(repo, worktree, branch)
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
        brief=brief, report=report, log=log, command=command, base_commit=base_commit,
    )
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
    created = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(worktree), "bash", "-lc", wrapper],
        text=True, capture_output=True,
    )
    if created.returncode:
        record_path.unlink(missing_ok=True)
        detail = created.stderr.strip() or created.stdout.strip()
        raise SystemExit(detail or "tmux failed to start launch-exec")
    subprocess.run(["tmux", "set-option", "-t", session, "remain-on-exit", "on"], capture_output=True)
    pane = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session, "#{pane_pid}"], text=True, capture_output=True
    )
    if pane.returncode == 0 and pane.stdout.strip().isdigit():
        record["pid"] = int(pane.stdout.strip())
        write_runtime_record(record_path, record)
    print(f"Launched: {task.path.name}")
    print(f"Attach: tmux attach -t {session}")
    print(f"Log: {log}")


def command_finish_runtime(repo: Path, plan: str, run_id: str, task_name: str, exit_code: int) -> None:
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


def status_entry(
    *, plan: str, run_id: str, task_name: str, run: Path, record: dict[str, object] | None,
    tmux_alive: callable, stale_after: timedelta, now: datetime,
) -> dict[str, object]:
    if record is None or not is_valid_runtime_record(record):
        return {"plan": plan, "run_id": run_id, "task": task_name, "state": "UNKNOWN", "elapsed": None,
                "session": None, "last_activity": None,
                "recovery_hint": "Inspect the legacy run ledger and task artifacts before relaunching manually."}
    report = resolve_artifact(run, record["report"])
    log = resolve_artifact(run, record["log"])
    report_status = final_report_status(report)
    alive = tmux_alive(str(record["session"]))
    started = parse_timestamp(record["started_at"])
    finished = parse_timestamp(record["finished_at"])
    elapsed_seconds = int(((finished or now) - started).total_seconds()) if started else None
    activities = [path.stat().st_mtime for path in (report, log) if path and path.exists()]
    activities.append((run / "runtime" / f"{Path(task_name).stem}.json").stat().st_mtime)
    last_activity = datetime.fromtimestamp(max(activities), UTC).isoformat() if activities else None
    if alive:
        state, hint = "RUNNING", f"Attach: tmux attach -t {record['session']}"
    elif record["exit_code"] == 0 and report_status == "DONE":
        state, hint = "SUCCEEDED_AWAITING_REVIEW", f"Open report: {report}"
    elif record["exit_code"] not in (None, 0) or report_status in {"DONE_WITH_CONCERNS", "NEEDS_CONTEXT", "BLOCKED"}:
        state, hint = "NEEDS_ATTENTION", f"Inspect log: {log}; investigate or relaunch manually."
    elif started and now - started > stale_after:
        state, hint = "STALE", f"Inspect log: {log}; investigate or relaunch manually."
    else:
        state, hint = "UNKNOWN", f"Inspect runtime record and log: {log}"
    return {"plan": plan, "run_id": run_id, "task": task_name, "state": state,
            "elapsed": elapsed_seconds, "session": record["session"], "last_activity": last_activity,
            "recovery_hint": hint}


def collect_status(
    repo: Path, plan: str | None = None, run_id: str | None = None, task_name: str | None = None,
    *, tmux_alive: callable = tmux_session_exists, stale_after: timedelta = timedelta(minutes=30),
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
            names = set(records) | tasks
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
    headers = ("PLAN", "RUN", "TASK", "STATE", "ELAPSED", "TMUX", "LAST ACTIVITY", "NEXT STEP")
    rows = [headers] + [(
        str(entry["plan"]), str(entry["run_id"]), str(entry["task"]), str(entry["state"]),
        format_elapsed(entry["elapsed"]), str(entry["session"] or "-"), str(entry["last_activity"] or "-"),
        str(entry["recovery_hint"]),
    ) for entry in entries]
    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    for index, row in enumerate(rows):
        print("  ".join(value.ljust(widths[column]) for column, value in enumerate(row)))
        if index == 0:
            print("  ".join("-" * width for width in widths))
    if not entries:
        print("No active task executions found.")


def command_status(repo: Path, plan: str | None, run_id: str | None, task_name: str | None, as_json: bool, watch: bool, interval: float) -> None:
    if interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    try:
        while True:
            entries = collect_status(repo, plan, run_id, task_name)
            if as_json:
                print(json.dumps({"tasks": entries}, indent=2, sort_keys=True))
                return
            if watch:
                print("\033[2J\033[H", end="")
                print("Task Graph status (Ctrl-C to stop)\n")
            print_status(entries)
            if not watch:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        if watch:
            print()


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
        choices=("archive-diff", "board", "plan", "reserve", "start", "done", "launch-exec", "finish-runtime", "status"),
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
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON where supported")
    parser.add_argument("--worktree", type=Path, help="Dedicated task worktree for launch-exec")
    parser.add_argument("--exit-code", type=int, help="Wrapper exit code for finish-runtime")
    parser.add_argument("--watch", action="store_true", help="Continuously redraw status")
    parser.add_argument("--interval", type=float, default=2.0, help="Status refresh interval in seconds")
    args = parser.parse_args()

    repo = args.repo.resolve()
    if args.command == "status":
        command_status(repo, args.plan, args.run_id, args.task, args.json, args.watch, args.interval)
        return
    if not args.plan:
        raise SystemExit(f"{args.command} requires --plan <plan-slug>")
    if args.command == "board":
        print(f"Board: {rewrite_board(repo, args.plan)}")
    elif args.command == "plan":
        schedule = schedule_tasks(repo, args.plan, args.limit)
        if args.json:
            print_schedule_json(schedule, args.limit)
        else:
            print_schedule(schedule, args.limit)
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


if __name__ == "__main__":
    main()
