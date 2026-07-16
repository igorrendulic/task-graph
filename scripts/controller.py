#!/usr/bin/env python3
"""Tmux-resident local Task Graph controller.

This module deliberately owns orchestration only.  Task and run state remains
in :mod:`kanban`, so an interrupted controller can recover from durable board,
runtime, wake, review, and delivery artifacts.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import kanban as KANBAN


STATE_VERSION = 2
CHECKPOINT_SECONDS = 60
HEARTBEAT_MAX_AGE_SECONDS = CHECKPOINT_SECONDS * 2
HUMAN_REQUIRED_ACTIONS = frozenset({"INSPECTION_REQUIRED", "USER_CONTEXT_REQUIRED", "RETRY_DECISION_REQUIRED"})
EXTERNAL_POLL_SECONDS = 1
NO_MISTAKES_TIMEOUT_SECONDS = 30 * 60
PUSH_TIMEOUT_SECONDS = 5 * 60
PR_LOOKUP_TIMEOUT_SECONDS = 60
PR_CHECKS_TIMEOUT_SECONDS = 30 * 60
MERGE_TIMEOUT_SECONDS = 5 * 60
FAILURE_JOURNAL_LIMIT = 50
FAILURE_TRACEBACK_MAX_CHARS = 4_000


@dataclass(frozen=True)
class ExternalRun:
    completed: subprocess.CompletedProcess[str] | None
    timed_out: bool = False
    missing: bool = False


class WakeDispatchFailure(Exception):
    """Preserve the wake that was active when dispatch raised unexpectedly."""

    def __init__(self, wake_id: str, error: Exception):
        super().__init__(str(error))
        self.wake_id = wake_id
        self.error = error


def controller_state_path(repo: Path, plan: str) -> Path:
    return KANBAN.plan_dir(repo, plan) / "state" / "controller.json"


def controller_failure_journal_path(repo: Path, plan: str) -> Path:
    return KANBAN.plan_dir(repo, plan) / "state" / "controller-failures.jsonl"


def controller_session_name(plan: str) -> str:
    return f"task-graph-controller-{plan}"


@contextmanager
def controller_lease_lock(repo: Path, plan: str):
    """Hold startup ownership until the tmux lease is durably recorded."""
    path = KANBAN.plan_dir(repo, plan) / "state" / "controller.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def controller_state_lock(repo: Path, plan: str):
    """Serialize durable controller state independently of startup ownership."""
    path = KANBAN.plan_dir(repo, plan) / "state" / "controller-state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_state(repo: Path, plan: str, state: dict[str, Any]) -> None:
    KANBAN.write_atomic(controller_state_path(repo, plan), json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_state(repo: Path, plan: str) -> dict[str, Any] | None:
    try:
        state = json.loads(controller_state_path(repo, plan).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid controller state: {error}") from None
    if not isinstance(state, dict) or state.get("version") not in {1, STATE_VERSION}:
        raise SystemExit("Invalid controller state")
    return normalize_state(state)


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Keep v1 controller records readable while adding durable health fields."""
    state["version"] = STATE_VERSION
    state.setdefault("heartbeat_at", None)
    state.setdefault("lease", None)
    state.setdefault("pending_alert", None)
    state.setdefault("recovery_alert", None)
    state.setdefault("recovery_required", False)
    state.setdefault("dispatches", {})
    state.setdefault("delivery_attempt", None)
    state.setdefault("active_failure", None)
    state.setdefault("revision", 0)
    return state


def create_state(repo: Path, plan: str, no_mistakes_command: str | None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "version": STATE_VERSION,
        "plan": plan,
        "session": controller_session_name(plan),
        "pid": None,
        "lifecycle": "running",
        "no_mistakes_command": no_mistakes_command,
        "dispatches": {},
        "heartbeat_at": KANBAN.utc_now(),
        "lease": {"session": controller_session_name(plan), "acquired_at": KANBAN.utc_now()},
        "pending_alert": None,
        "recovery_alert": None,
        "recovery_required": False,
        "active_failure": None,
        "updated_at": KANBAN.utc_now(),
    }
    state["revision"] = 1
    with controller_state_lock(repo, plan):
        write_state(repo, plan, state)
    return state


def persist_mutation(repo: Path, plan: str, state: dict[str, Any]) -> None:
    state["revision"] = int(state["revision"]) + 1
    state["updated_at"] = KANBAN.utc_now()
    write_state(repo, plan, state)


def mutate_state(repo: Path, plan: str, mutation: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Reload, mutate, and atomically persist controller state under one lock."""
    with controller_state_lock(repo, plan):
        state = load_state(repo, plan)
        if state is None:
            raise SystemExit("Controller has not been started")
        mutation(state)
        persist_mutation(repo, plan, state)
        return state


def record_controller_failure(
    repo: Path, plan: str, *, phase: str, error: BaseException, wake_id: str | None
) -> dict[str, object]:
    """Persist the latest unexpected controller failure without touching wakes."""
    trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-FAILURE_TRACEBACK_MAX_CHARS:]
    failure: dict[str, object] = {
        "timestamp": KANBAN.utc_now(),
        "phase": phase,
        "exception_type": type(error).__name__,
        "message": str(error),
        "wake_id": wake_id,
        "traceback": trace,
    }
    with controller_state_lock(repo, plan):
        journal = controller_failure_journal_path(repo, plan)
        try:
            entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        except FileNotFoundError:
            entries = []
        entries.append(failure)
        KANBAN.write_atomic(journal, "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries[-FAILURE_JOURNAL_LIMIT:]))
        state = load_state(repo, plan)
        if state is None:
            raise SystemExit("Controller has not been started")
        state["active_failure"] = failure
        persist_mutation(repo, plan, state)
    return failure


def replace_state(snapshot: dict[str, Any], current: dict[str, Any]) -> None:
    snapshot.clear()
    snapshot.update(current)


def refresh_heartbeat(repo: Path, plan: str, state: dict[str, Any]) -> None:
    current = mutate_state(repo, plan, lambda latest: latest.__setitem__("heartbeat_at", KANBAN.utc_now()))
    replace_state(state, current)


def last_dispatch_wake_id(state: dict[str, Any]) -> str | None:
    """Return the most recently persisted dispatch identity, if any."""
    dispatches = state.get("dispatches")
    if not isinstance(dispatches, dict):
        return None
    for wake_id in reversed(dispatches):
        if isinstance(wake_id, str):
            return wake_id
    return None


def heartbeat_is_fresh(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        age = (datetime.now().astimezone() - datetime.fromisoformat(value)).total_seconds()
        return 0 <= age <= HEARTBEAT_MAX_AGE_SECONDS
    except ValueError:
        return False


def has_in_progress_tasks(repo: Path, plan: str) -> bool:
    return any(task.column == "in-progress" for task in KANBAN.read_tasks_readonly(repo, plan))


def pause_for_alert(repo: Path, plan: str, state: dict[str, Any], wake: dict[str, object], reason: str) -> None:
    wake_id = str(wake["id"])
    def pause(latest: dict[str, Any]) -> None:
        latest.setdefault("dispatches", {})[wake_id] = {"state": "escalated", "wake": wake, "result": reason}
        latest["pending_alert"] = {
            "wake_id": wake_id, "task": str(wake.get("task", "")), "run_id": str(wake.get("run_id", "")),
            "reason": reason, "at": KANBAN.utc_now(),
        }
        latest["lifecycle"] = "paused"

    replace_state(state, mutate_state(repo, plan, pause))
    KANBAN.escalate_wake(repo, plan, wake_id)
    print(f"Controller paused: {reason} for {wake.get('task', 'unknown task')}")


def pause_for_supervision_corruption(
    repo: Path, plan: str, state: dict[str, Any], error: KANBAN.SupervisionStateCorruption
) -> None:
    """Persist a fail-closed alert without changing the corrupt coordination state."""
    def pause(latest: dict[str, Any]) -> None:
        latest["lifecycle"] = "paused"
        latest["pending_alert"] = {
            "reason": "SUPERVISION_STATE_CORRUPTION",
            "artifact": str(error.path),
            "line": error.line,
            "detail": error.detail,
            "at": KANBAN.utc_now(),
        }

    replace_state(state, mutate_state(repo, plan, pause))
    print(f"Controller paused: SUPERVISION_STATE_CORRUPTION at {error.path}")


def pending_alert_resolved(repo: Path, plan: str, alert: object) -> bool:
    if not isinstance(alert, dict):
        return True
    task, reason = str(alert.get("task", "")), str(alert.get("reason", ""))
    if reason == "SUPERVISION_STATE_CORRUPTION":
        return True
    if task not in {item.path.name for item in KANBAN.read_tasks_readonly(repo, plan) if item.column == "in-progress"}:
        return True
    if reason in HUMAN_REQUIRED_ACTIONS:
        return not any(str(action.get("task")) == task and str(action.get("action")) == reason for action in KANBAN.reconcile_actions(repo, plan))
    return False


def reconcile_pending_alert(repo: Path, plan: str, state: dict[str, Any]) -> bool:
    alert = state.get("pending_alert")
    if alert is None:
        return True
    if not pending_alert_resolved(repo, plan, alert):
        return False
    replace_state(state, mutate_state(repo, plan, lambda latest: latest.__setitem__("pending_alert", None)))
    return True


def require_tmux() -> None:
    if not shutil.which("tmux"):
        raise SystemExit("tmux is required for controller")


def tmux_start(session: str, cwd: Path, command: str) -> int | None:
    created = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(cwd), "bash", "-lc", command],
        text=True,
        capture_output=True,
    )
    if created.returncode:
        raise RuntimeError(created.stderr.strip() or created.stdout.strip() or "tmux failed to start session")
    subprocess.run(["tmux", "set-option", "-t", session, "remain-on-exit", "on"], capture_output=True)
    pane = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session, "#{pane_pid}"], text=True, capture_output=True
    )
    return int(pane.stdout.strip()) if pane.returncode == 0 and pane.stdout.strip().isdigit() else None


def latest_runtime(repo: Path, plan: str, task: str) -> tuple[str, Path, dict[str, object]]:
    runtime = KANBAN.latest_runtime_record(repo, plan, task)
    if runtime is None:
        raise RuntimeError(f"No runtime record for {task}")
    _, run, record = runtime
    return run.name, run, record


def start_review(repo: Path, plan: str, wake: dict[str, object], state: dict[str, Any] | None = None) -> str:
    """Persist pending review and launch a fresh read-only reviewer."""
    require_tmux()
    task = str(wake["task"])
    run_id, run, _ = latest_runtime(repo, plan, task)
    prepared = KANBAN.prepare_completed_task(repo, plan, run_id, task)
    record = prepared["record"]
    review = run / "reviews" / task
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("Review status: pending\n", encoding="utf-8")
    KANBAN.command_archive_diff(
        repo, plan, run_id, task, str(prepared["base_commit"]), str(prepared["head_commit"]),
        str(prepared["branch"]), f"reviews/{task}",
    )
    session = f"task-graph-review-{plan}-{run_id}-{Path(task).stem}"
    log = run / "logs" / f"{Path(task).stem}.review.log"
    prompt = (
        f"Review the completed task {task} read-only. Inspect its diff and report. Write exactly one first line "
        "`Review status: approved` or `Review status: changes_requested`, then concise findings and test evidence."
    )
    command = " ".join(
        shlex.quote(value)
        for value in ["codex", "exec", "--sandbox", "read-only", "--output-last-message", str(review), prompt]
    )
    tmux_start(session, Path(str(record["worktree"])), f"set -o pipefail; {command} 2>&1 | tee -a {shlex.quote(str(log))}")
    return session


def repair_run_id(parent_run: str, task: str, attempt: int) -> str:
    return f"{parent_run}-task{Path(task).stem.split('-', 1)[0]}-repair{attempt}"


def start_repair(repo: Path, plan: str, wake: dict[str, object], state: dict[str, Any] | None = None) -> str:
    task = str(wake["task"])
    reservation = KANBAN.repair_attempt(repo, plan, task)
    if reservation is not None:
        existing = KANBAN.runtime_record_for_run(repo, plan, str(reservation["child_run_id"]), task)
        if existing is not None:
            KANBAN.mark_repair_attempt_phase(repo, plan, task, "launched")
            return str(existing[2].get("session", reservation["branch"]))
        if reservation["phase"] == "launched":
            return "INSPECTION_REQUIRED"

    require_tmux()
    parent_run_id, parent_run, parent = latest_runtime(repo, plan, task)
    if reservation is None:
        attempt = KANBAN.repair_attempts(repo, plan, task) + 1
        child_run_id = repair_run_id(parent_run_id, task, attempt)
        branch = f"{parent['branch']}-repair-{attempt}"
        worktree = Path(tempfile.gettempdir()) / f"task-graph-{plan}-{child_run_id}-{Path(task).stem}"
        reservation = KANBAN.reserve_repair_attempt(
            repo, plan, task, attempt=attempt, child_run_id=child_run_id, branch=branch, worktree=worktree
        )
    child_run_id = str(reservation["child_run_id"])
    branch = str(reservation["branch"])
    worktree = Path(str(reservation["worktree"]))
    policy = KANBAN.read_run_policy(repo, plan, parent_run_id)
    child = KANBAN.ensure_run_dirs(repo, plan, child_run_id)
    policy_path = KANBAN.policy_path(repo, plan, child_run_id)
    if not policy_path.exists():
        KANBAN.write_atomic(policy_path, json.dumps(policy) + "\n")
    review = parent_run / "reviews" / task
    concerns = review.read_text(encoding="utf-8") if review.exists() else "Worker reported concerns; inspect prior report.\n"
    brief = child / "briefs" / task
    if not brief.exists():
        brief.write_text(f"# Focused repair: {task}\n\n{concerns}", encoding="utf-8")
    try:
        if worktree.exists():
            KANBAN.verified_worktree(repo, worktree, branch)
        else:
            KANBAN.create_child_worktree(repo, str(parent["branch"]), branch, worktree)
        KANBAN.command_launch_exec(repo, plan, child_run_id, task, branch, worktree)
    except SystemExit:
        if worktree.exists():
            return "INSPECTION_REQUIRED"
        KANBAN.mark_repair_attempt_phase(repo, plan, task, "failed")
        return "REPAIR_REQUIRED"
    runtime = KANBAN.runtime_record_for_run(repo, plan, child_run_id, task)
    if runtime is None:
        return "INSPECTION_REQUIRED"
    KANBAN.mark_repair_attempt_phase(repo, plan, task, "launched")
    return str(runtime[2].get("session", branch))


def finalize_landed(repo: Path, plan: str, run_id: str, task: str) -> None:
    KANBAN.command_record_delivery(repo, plan, run_id, task, "landed")
    KANBAN.command_teardown(repo, plan, run_id, task, discard=False)
    KANBAN.command_done(repo, plan, task)


def run_external(
    command: list[str] | str, *, cwd: Path, timeout_seconds: int, heartbeat: Callable[[], None] | None = None,
    shell: bool = False,
) -> ExternalRun:
    """Poll a bounded external command while allowing the controller to stay healthy."""
    try:
        process = subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=shell)
    except FileNotFoundError:
        return ExternalRun(None, missing=True)
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None:
        if heartbeat is not None:
            heartbeat()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            try:
                process.communicate(timeout=EXTERNAL_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            return ExternalRun(None, timed_out=True)
        time.sleep(min(EXTERNAL_POLL_SECONDS, remaining))
    stdout, stderr = process.communicate()
    return ExternalRun(subprocess.CompletedProcess(command, process.returncode, stdout, stderr))


def delivery_heartbeat(repo: Path, plan: str, state: dict[str, Any]) -> Callable[[], None]:
    return lambda: refresh_heartbeat(repo, plan, state)


def record_merge_submission(
    repo: Path, plan: str, state: dict[str, Any], *, task: str, run_id: str, branch: str, expected_head: str,
) -> None:
    def record(latest: dict[str, Any]) -> None:
        latest["delivery_attempt"] = {
            "task": task, "run_id": run_id, "branch": branch, "expected_head": expected_head,
            "state": "submitted", "submitted_at": KANBAN.utc_now(),
        }

    replace_state(state, mutate_state(repo, plan, record))


def reconcile_delivery_outcome(repo: Path, plan: str, state: dict[str, Any]) -> bool:
    """Finalize only a durably recorded submission that GitHub confirms as merged."""
    attempt = state.get("delivery_attempt")
    if not isinstance(attempt, dict) or attempt.get("state") == "finalized":
        return True
    branch, expected_head = attempt.get("branch"), attempt.get("expected_head")
    if not isinstance(branch, str) or not isinstance(expected_head, str) or not expected_head:
        return False
    result = run_external(
        ["gh", "pr", "view", branch, "--json", "state,headRefOid"], cwd=repo,
        timeout_seconds=PR_LOOKUP_TIMEOUT_SECONDS, heartbeat=delivery_heartbeat(repo, plan, state),
    )
    if result.timed_out or result.missing or result.completed is None or result.completed.returncode:
        return False
    try:
        outcome = json.loads(result.completed.stdout)
    except json.JSONDecodeError:
        return False
    if not isinstance(outcome, dict) or outcome.get("state") != "MERGED" or outcome.get("headRefOid") != expected_head:
        return False
    finalize_landed(repo, plan, str(attempt["run_id"]), str(attempt["task"]))

    def finalized(latest: dict[str, Any]) -> None:
        current = latest.get("delivery_attempt")
        if isinstance(current, dict):
            current["state"] = "finalized"
            current["finalized_at"] = KANBAN.utc_now()
        latest["pending_alert"] = None

    replace_state(state, mutate_state(repo, plan, finalized))
    return True


def deliver(repo: Path, plan: str, wake: dict[str, object], state: dict[str, Any]) -> str:
    task, run_id = str(wake["task"]), str(wake["run_id"])
    attempt = state.get("delivery_attempt")
    if isinstance(attempt, dict) and attempt.get("state") == "submitted":
        return "DELIVERY_OUTCOME_UNKNOWN"
    action = KANBAN.command_delivery_ready(repo, plan, run_id, task)
    _, _, record = latest_runtime(repo, plan, task)
    policy = KANBAN.read_run_policy(repo, plan, run_id)
    if action in {"AWAIT_LOCAL_APPROVAL", "OPEN_PR"} or not policy["yolo"]:
        return "DELIVERY_APPROVAL_REQUIRED"
    if action == "RUN_NO_MISTAKES":
        command = state.get("no_mistakes_command")
        if not isinstance(command, str) or not command.strip():
            return "NO_MISTAKES_COMMAND_REQUIRED"
        gate = run_external(
            command, cwd=Path(str(record["worktree"])), timeout_seconds=NO_MISTAKES_TIMEOUT_SECONDS,
            heartbeat=delivery_heartbeat(repo, plan, state), shell=True,
        )
        if gate.missing:
            return "TOOLING_REQUIRED"
        if gate.timed_out:
            return "NO_MISTAKES_TIMEOUT"
        if gate.completed is None or gate.completed.returncode:
            return "NO_MISTAKES_FAILED"
        action = "MERGE_GREEN_PR"
    if action == "FAST_FORWARD_LOCAL":
        if KANBAN.git_output(repo, "status", "--porcelain").strip():
            return "LOCAL_DELIVERY_DIRTY"
        result = subprocess.run(["git", "merge", "--ff-only", str(record["branch"])], cwd=repo, text=True, capture_output=True)
        if result.returncode:
            return "LOCAL_DELIVERY_NOT_FAST_FORWARD"
        finalize_landed(repo, plan, run_id, task)
        return "LANDED"
    if action == "MERGE_GREEN_PR":
        branch = str(record["branch"])
        heartbeat = delivery_heartbeat(repo, plan, state)
        pushed = run_external(["git", "push", "-u", "origin", branch], cwd=repo, timeout_seconds=PUSH_TIMEOUT_SECONDS, heartbeat=heartbeat)
        if pushed.missing:
            return "TOOLING_REQUIRED"
        if pushed.timed_out:
            return "PUSH_TIMEOUT"
        if pushed.completed is None or pushed.completed.returncode:
            return "PR_DELIVERY_FAILED"
        existing = run_external(["gh", "pr", "view", branch, "--json", "number"], cwd=repo, timeout_seconds=PR_LOOKUP_TIMEOUT_SECONDS, heartbeat=heartbeat)
        if existing.missing:
            return "TOOLING_REQUIRED"
        if existing.timed_out:
            return "PR_LOOKUP_TIMEOUT"
        if existing.completed is None or existing.completed.returncode:
            created = run_external(["gh", "pr", "create", "--head", branch, "--fill"], cwd=repo, timeout_seconds=PUSH_TIMEOUT_SECONDS, heartbeat=heartbeat)
            if created.missing:
                return "TOOLING_REQUIRED"
            if created.timed_out:
                return "PR_CREATE_TIMEOUT"
            if created.completed is None or created.completed.returncode:
                return "PR_DELIVERY_FAILED"
        checks = run_external(["gh", "pr", "checks", branch, "--watch", "--fail-fast"], cwd=repo, timeout_seconds=PR_CHECKS_TIMEOUT_SECONDS, heartbeat=heartbeat)
        if checks.missing:
            return "TOOLING_REQUIRED"
        if checks.timed_out:
            return "PR_CHECKS_TIMEOUT"
        if checks.completed is None or checks.completed.returncode:
            return "PR_DELIVERY_FAILED"
        expected_head = str(record.get("head_commit", ""))
        if not expected_head:
            return "DELIVERY_HEAD_REQUIRED"
        record_merge_submission(repo, plan, state, task=task, run_id=run_id, branch=branch, expected_head=expected_head)
        try:
            merged = run_external(
                ["gh", "pr", "merge", branch, "--merge", "--delete-branch"], cwd=repo,
                timeout_seconds=MERGE_TIMEOUT_SECONDS, heartbeat=heartbeat,
            )
        except (OSError, RuntimeError):
            return "DELIVERY_OUTCOME_UNKNOWN"
        if merged.missing:
            return "DELIVERY_OUTCOME_UNKNOWN"
        if merged.timed_out:
            return "DELIVERY_OUTCOME_UNKNOWN"
        if merged.completed is None or merged.completed.returncode:
            return "DELIVERY_OUTCOME_UNKNOWN"
        finalize_landed(repo, plan, run_id, task)
        replace_state(state, mutate_state(repo, plan, lambda latest: latest.__setitem__("delivery_attempt", None)))
        return "LANDED"
    return "DELIVERY_APPROVAL_REQUIRED"


def dispatch_wake(repo: Path, plan: str, wake: dict[str, object], state: dict[str, Any], *, claimed: bool = False) -> str:
    wake_id = str(wake["id"])
    if not claimed:
        wake = KANBAN.claim_wake(repo, plan, wake_id)
        replace_state(
            state,
            mutate_state(
                repo, plan, lambda latest: latest.setdefault("dispatches", {}).__setitem__(wake_id, {"state": "claimed", "wake": wake})
            ),
        )
    action = str(wake["action"])
    if action in HUMAN_REQUIRED_ACTIONS:
        pause_for_alert(repo, plan, state, wake, action)
        return action
    if action == "REVIEW_REQUIRED":
        result = start_review(repo, plan, wake, state)
    elif action == "REPAIR_REQUIRED":
        result = start_repair(repo, plan, wake, state)
    elif action == "DELIVERY_REQUIRED":
        try:
            result = deliver(repo, plan, wake, state)
        except (OSError, RuntimeError, SystemExit):
            result = "DELIVERY_FAILED"
    else:
        result = action
    if result != "LANDED" and action == "DELIVERY_REQUIRED":
        pause_for_alert(repo, plan, state, wake, result)
        return result
    if result == "INSPECTION_REQUIRED":
        pause_for_alert(repo, plan, state, wake, result)
        return result
    replace_state(
        state,
        mutate_state(
            repo, plan, lambda latest: latest.setdefault("dispatches", {}).__setitem__(
                wake_id, {"state": "started", "wake": wake, "result": result}
            )
        ),
    )
    KANBAN.acknowledge_wake(repo, plan, wake_id)
    replace_state(
        state,
        mutate_state(
            repo, plan, lambda latest: latest.setdefault("dispatches", {}).setdefault(wake_id, {}).__setitem__("state", "acknowledged")
        ),
    )
    return result


def dispatch_wake_with_context(
    repo: Path, plan: str, wake: dict[str, object], state: dict[str, Any], *, claimed: bool = False
) -> str:
    """Dispatch one wake while retaining its identity if the dispatch fails."""
    try:
        return dispatch_wake(repo, plan, wake, state, claimed=claimed)
    except Exception as error:
        raise WakeDispatchFailure(str(wake["id"]), error) from error


def resume_claimed_wakes(repo: Path, plan: str, state: dict[str, Any]) -> None:
    for wake_id, entry in list(state.get("dispatches", {}).items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("wake"), dict):
            continue
        if entry.get("state") == "claimed":
            dispatch_wake_with_context(repo, plan, entry["wake"], state, claimed=True)
        elif entry.get("state") == "started":
            KANBAN.acknowledge_wake(repo, plan, str(wake_id))
            replace_state(
                state,
                mutate_state(
                    repo,
                    plan,
                    lambda latest: latest.setdefault("dispatches", {}).setdefault(str(wake_id), {}).__setitem__("state", "acknowledged"),
                ),
            )


def reconcile_reviewer_dispatches(repo: Path, plan: str, state: dict[str, Any]) -> None:
    """Turn an exited reviewer without a machine-readable verdict into a checkpoint."""
    changed = False
    for entry in state.get("dispatches", {}).values():
        if not isinstance(entry, dict) or entry.get("state") != "acknowledged":
            continue
        wake = entry.get("wake")
        if not isinstance(wake, dict) or wake.get("action") != "REVIEW_REQUIRED":
            continue
        task, run_id = str(wake.get("task", "")), str(wake.get("run_id", ""))
        verdict = KANBAN.review_status(KANBAN.run_dir(repo, plan, run_id) / "reviews" / task)
        if verdict in {"approved", "changes_requested"}:
            if entry.get("result") != verdict:
                entry["result"] = verdict
                changed = True
            continue
        session = entry.get("result")
        if isinstance(session, str) and KANBAN.tmux_liveness(session) != "RUNNING":
            if entry.get("result") != "REVIEW_VERDICT_REQUIRED":
                entry["result"] = "REVIEW_VERDICT_REQUIRED"
                changed = True
    if changed:
        results = {
            str(wake_id): str(entry["result"])
            for wake_id, entry in state.get("dispatches", {}).items()
            if isinstance(entry, dict) and entry.get("state") == "acknowledged" and isinstance(entry.get("result"), str)
        }

        def update_results(latest: dict[str, Any]) -> None:
            for wake_id, result in results.items():
                entry = latest.setdefault("dispatches", {}).get(wake_id)
                if isinstance(entry, dict):
                    entry["result"] = result

        replace_state(state, mutate_state(repo, plan, update_results))


def drain_once(repo: Path, plan: str, state: dict[str, Any]) -> list[str]:
    try:
        claims = KANBAN.load_supervision_json_object(KANBAN.wake_claims_path(repo, plan))
        KANBAN.load_supervision_json_object(KANBAN.supervision_index_path(repo, plan))
        wakes = KANBAN.queued_wakes(repo, plan)
        resume_claimed_wakes(repo, plan, state)
        reconcile_reviewer_dispatches(repo, plan, state)
        KANBAN.supervise_once(repo, plan)
        claims = KANBAN.load_supervision_json_object(KANBAN.wake_claims_path(repo, plan))
        wakes = KANBAN.queued_wakes(repo, plan)
        results: list[str] = []
        for wake in wakes:
            wake_id = str(wake["id"])
            if claims.get(wake_id) in {"acknowledged", "escalated"}:
                continue
            if claims.get(wake_id) == "claimed":
                if state.get("dispatches", {}).get(wake_id, {}).get("state") == "claimed":
                    results.append(dispatch_wake_with_context(repo, plan, wake, state, claimed=True))
                    if state.get("lifecycle") != "running":
                        break
                continue
            results.append(dispatch_wake_with_context(repo, plan, wake, state))
            if state.get("lifecycle") != "running":
                break
        return results
    except KANBAN.SupervisionStateCorruption as error:
        pause_for_supervision_corruption(repo, plan, state, error)
        return []


def run_controller(repo: Path, plan: str) -> int:
    state = load_state(repo, plan)
    if state is None:
        raise SystemExit("Controller has not been started")
    while state.get("lifecycle") == "running":
        for phase, operation in (
            ("heartbeat", lambda: refresh_heartbeat(repo, plan, state)),
            ("drain", lambda: drain_once(repo, plan, state)),
            ("supervise", lambda: KANBAN.command_supervise(repo, plan, CHECKPOINT_SECONDS)),
        ):
            if phase == "supervise" and state.get("lifecycle") != "running":
                break
            try:
                operation()
                if phase == "supervise":
                    state = load_state(repo, plan) or state
            except WakeDispatchFailure as failure:
                failure = record_controller_failure(
                    repo, plan, phase=phase, error=failure.error, wake_id=failure.wake_id
                )
                print(
                    f"Controller failed during {phase}: {failure['exception_type']}: {failure['message']}",
                    file=sys.stderr,
                )
                return 1
            except Exception as error:
                failure = record_controller_failure(
                    repo, plan, phase=phase, error=error, wake_id=last_dispatch_wake_id(state)
                )
                print(
                    f"Controller failed during {phase}: {failure['exception_type']}: {failure['message']}",
                    file=sys.stderr,
                )
                return 1
        else:
            continue
        break
    return 0


def start_controller(repo: Path, plan: str, no_mistakes_command: str | None) -> None:
    require_tmux()
    with controller_lease_lock(repo, plan):
        state = load_state(repo, plan)
        if state and KANBAN.tmux_session_exists(str(state.get("session"))):
            raise SystemExit("A live controller already exists for this plan")
        state = create_state(repo, plan, no_mistakes_command) if state is None else state
        if not reconcile_delivery_outcome(repo, plan, state):
            raise SystemExit("Pending controller alert: DELIVERY_OUTCOME_UNKNOWN")
        if not reconcile_pending_alert(repo, plan, state):
            alert = state.get("pending_alert") or {}
            raise SystemExit(f"Pending controller alert: {alert.get('reason', 'human action required')}")
        def acquire_lease(latest: dict[str, Any]) -> None:
            latest["lifecycle"] = "running"
            latest["lease"] = {"session": controller_session_name(plan), "acquired_at": KANBAN.utc_now()}
            latest["recovery_required"] = False
            latest["recovery_alert"] = None
            if no_mistakes_command is not None:
                latest["no_mistakes_command"] = no_mistakes_command

        state = mutate_state(repo, plan, acquire_lease)
        command = " ".join(shlex.quote(value) for value in [sys.executable, str(Path(__file__).resolve()), "run", "--repo", str(repo), "--plan", plan])
        # Persist ownership before creating tmux so a crash cannot leave an unowned controller.
        state["pid"] = tmux_start(str(state["session"]), repo, command)
        pid = state["pid"]
        state = mutate_state(repo, plan, lambda latest: latest.__setitem__("pid", pid))
        state = mutate_state(repo, plan, lambda latest: latest.__setitem__("active_failure", None))
        print(f"Started controller: {state['session']}")


def status_controller(repo: Path, plan: str) -> None:
    state = load_state(repo, plan)
    if state is None:
        raise SystemExit("Controller has not been started")
    projection = json.loads(json.dumps(state))
    projection["live"] = KANBAN.tmux_session_exists(str(projection["session"]))
    projection["healthy"] = bool(projection["live"] and heartbeat_is_fresh(projection.get("heartbeat_at")))
    projection["recovery_required"] = bool(
        projection.get("lifecycle") == "running" and has_in_progress_tasks(repo, plan) and not projection["healthy"]
    )
    if projection["recovery_required"]:
        projection["recovery_alert"] = {"reason": "CONTROLLER_RECOVERY_REQUIRED", "at": KANBAN.utc_now()}
    else:
        projection["recovery_alert"] = None
    if not projection["healthy"] and (projection.get("active_failure") or projection["recovery_required"]):
        projection["recovery_recommendation"] = "Inspect active_failure and explicitly run controller.py start."
    else:
        projection["recovery_recommendation"] = None
    print(json.dumps(projection, indent=2, sort_keys=True))


def stop_controller(repo: Path, plan: str) -> None:
    with controller_state_lock(repo, plan):
        state = load_state(repo, plan)
        if state is None:
            raise SystemExit("Controller has not been started")
        subprocess.run(["tmux", "kill-session", "-t", str(state["session"])], text=True, capture_output=True)
        if KANBAN.tmux_session_exists(str(state["session"])):
            raise SystemExit("Controller session could not be terminated; lease retained")
        state["lifecycle"] = "stopped"
        state["lease"] = None
        state["recovery_required"] = False
        state["recovery_alert"] = None
        persist_mutation(repo, plan, state)
    print(f"Stopped controller: {state['session']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "run", "status", "stop"))
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--no-mistakes-command")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.command == "start":
        start_controller(repo, args.plan, args.no_mistakes_command)
    elif args.command == "run":
        run_controller(repo, args.plan)
    elif args.command == "status":
        status_controller(repo, args.plan)
    else:
        stop_controller(repo, args.plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
