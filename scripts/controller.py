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
from datetime import datetime
from contextlib import contextmanager
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


def controller_state_path(repo: Path, plan: str) -> Path:
    return KANBAN.plan_dir(repo, plan) / "state" / "controller.json"


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


def replace_state(snapshot: dict[str, Any], current: dict[str, Any]) -> None:
    snapshot.clear()
    snapshot.update(current)


def refresh_heartbeat(repo: Path, plan: str, state: dict[str, Any]) -> None:
    current = mutate_state(repo, plan, lambda latest: latest.__setitem__("heartbeat_at", KANBAN.utc_now()))
    replace_state(state, current)


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


def pending_alert_resolved(repo: Path, plan: str, alert: object) -> bool:
    if not isinstance(alert, dict):
        return True
    task, reason = str(alert.get("task", "")), str(alert.get("reason", ""))
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
    require_tmux()
    task = str(wake["task"])
    parent_run_id, parent_run, parent = latest_runtime(repo, plan, task)
    attempt = KANBAN.repair_attempts(repo, plan, task) + 1
    KANBAN.record_repair_attempt(repo, plan, task)
    child_run_id = repair_run_id(parent_run_id, task, attempt)
    policy = KANBAN.read_run_policy(repo, plan, parent_run_id)
    child = KANBAN.ensure_run_dirs(repo, plan, child_run_id)
    KANBAN.write_atomic(KANBAN.policy_path(repo, plan, child_run_id), json.dumps(policy) + "\n")
    review = parent_run / "reviews" / task
    concerns = review.read_text(encoding="utf-8") if review.exists() else "Worker reported concerns; inspect prior report.\n"
    brief = child / "briefs" / task
    brief.write_text(f"# Focused repair: {task}\n\n{concerns}", encoding="utf-8")
    branch = f"{parent['branch']}-repair-{attempt}"
    worktree = Path(tempfile.gettempdir()) / f"task-graph-{plan}-{child_run_id}-{Path(task).stem}"
    KANBAN.create_child_worktree(repo, str(parent["branch"]), branch, worktree)
    KANBAN.command_launch_exec(repo, plan, child_run_id, task, branch, worktree)
    runtime = KANBAN.latest_runtime_record(repo, plan, task)
    return str(runtime[2].get("session", branch)) if runtime else branch


def finalize_landed(repo: Path, plan: str, run_id: str, task: str) -> None:
    KANBAN.command_record_delivery(repo, plan, run_id, task, "landed")
    KANBAN.command_teardown(repo, plan, run_id, task, discard=False)
    KANBAN.command_done(repo, plan, task)


def run_external(command: list[str], *, cwd: Path, **kwargs: Any) -> subprocess.CompletedProcess[str] | None:
    """Run an integration tool without turning a missing binary into lost work."""
    try:
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True, **kwargs)
    except FileNotFoundError:
        return None


def deliver(repo: Path, plan: str, wake: dict[str, object], state: dict[str, Any]) -> str:
    task, run_id = str(wake["task"]), str(wake["run_id"])
    action = KANBAN.command_delivery_ready(repo, plan, run_id, task)
    _, _, record = latest_runtime(repo, plan, task)
    policy = KANBAN.read_run_policy(repo, plan, run_id)
    if action in {"AWAIT_LOCAL_APPROVAL", "OPEN_PR"} or not policy["yolo"]:
        return "DELIVERY_APPROVAL_REQUIRED"
    if action == "RUN_NO_MISTAKES":
        command = state.get("no_mistakes_command")
        if not isinstance(command, str) or not command.strip():
            return "NO_MISTAKES_COMMAND_REQUIRED"
        try:
            gate = subprocess.run(command, shell=True, cwd=Path(str(record["worktree"])))
        except FileNotFoundError:
            return "TOOLING_REQUIRED"
        if gate.returncode:
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
        pushed = run_external(["git", "push", "-u", "origin", branch], cwd=repo)
        if pushed is None:
            return "TOOLING_REQUIRED"
        if pushed.returncode:
            return "PR_DELIVERY_FAILED"
        existing = run_external(["gh", "pr", "view", branch, "--json", "number"], cwd=repo)
        if existing is None:
            return "TOOLING_REQUIRED"
        if existing.returncode:
            created = run_external(["gh", "pr", "create", "--head", branch, "--fill"], cwd=repo)
            if created is None:
                return "TOOLING_REQUIRED"
            if created.returncode:
                return "PR_DELIVERY_FAILED"
        for command in (["gh", "pr", "checks", branch, "--watch", "--fail-fast"], ["gh", "pr", "merge", branch, "--merge", "--delete-branch"]):
            result = run_external(command, cwd=repo)
            if result is None:
                return "TOOLING_REQUIRED"
            if result.returncode:
                return "PR_DELIVERY_FAILED"
        finalize_landed(repo, plan, run_id, task)
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


def resume_claimed_wakes(repo: Path, plan: str, state: dict[str, Any]) -> None:
    for wake_id, entry in list(state.get("dispatches", {}).items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("wake"), dict):
            continue
        if entry.get("state") == "claimed":
            dispatch_wake(repo, plan, entry["wake"], state, claimed=True)
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
    resume_claimed_wakes(repo, plan, state)
    reconcile_reviewer_dispatches(repo, plan, state)
    KANBAN.supervise_once(repo, plan)
    claims = KANBAN.load_json_object(KANBAN.wake_claims_path(repo, plan))
    results: list[str] = []
    for wake in KANBAN.queued_wakes(repo, plan):
        wake_id = str(wake.get("id", ""))
        if claims.get(wake_id) in {"acknowledged", "escalated"}:
            continue
        if claims.get(wake_id) == "claimed":
            if state.get("dispatches", {}).get(wake_id, {}).get("state") == "claimed":
                results.append(dispatch_wake(repo, plan, wake, state, claimed=True))
                if state.get("lifecycle") != "running":
                    break
            continue
        results.append(dispatch_wake(repo, plan, wake, state))
        if state.get("lifecycle") != "running":
            break
    return results


def run_controller(repo: Path, plan: str) -> int:
    state = load_state(repo, plan)
    if state is None:
        raise SystemExit("Controller has not been started")
    while state.get("lifecycle") == "running":
        refresh_heartbeat(repo, plan, state)
        drain_once(repo, plan, state)
        if state.get("lifecycle") != "running":
            break
        # Supervision owns the bounded wait and writes any wake before it returns.
        KANBAN.command_supervise(repo, plan, CHECKPOINT_SECONDS)
        state = load_state(repo, plan) or state
    return 0


def start_controller(repo: Path, plan: str, no_mistakes_command: str | None) -> None:
    require_tmux()
    with controller_lease_lock(repo, plan):
        state = load_state(repo, plan)
        if state and KANBAN.tmux_session_exists(str(state.get("session"))):
            raise SystemExit("A live controller already exists for this plan")
        state = create_state(repo, plan, no_mistakes_command) if state is None else state
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
