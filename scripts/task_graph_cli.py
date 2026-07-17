"""Command line entry points for Task Graph execution runs."""

from __future__ import annotations

import argparse
import shlex
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.task_graph_controller import TaskGraphController
from scripts.task_graph_display import TerminalDashboard
from scripts.task_graph_git import TaskGraphGit, TaskGraphGitError
from scripts.task_graph_notifications import notify_completion
from scripts.task_graph_runtime import (
    RunLock,
    TaskGraphRuntimeError,
    create_run_snapshot,
    create_state,
    ensure_clean_base,
    load_state,
    require_git_common_dir,
    write_state,
)
from scripts.task_graph_tmux import TmuxClient


DEFAULT_MAX_WORKERS = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute a Task Graph DAG")
    subcommands = parser.add_subparsers(dest="action", required=True)
    start = subcommands.add_parser("start", help="start a new plan run")
    start.add_argument("plan_slug")
    start.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    start.add_argument(
        "--worker-command",
        default="codex",
        help="worker executable recorded in run state (default: codex)",
    )
    resume = subcommands.add_parser("resume", help="reconnect or restart a plan controller")
    resume.add_argument("plan_slug")
    resume.add_argument("run_id")
    status = subcommands.add_parser("status", help="report the state of a plan run")
    status.add_argument("plan_slug")
    status.add_argument("--run-id")
    merge = subcommands.add_parser("merge", help="promote a successful plan run")
    merge.add_argument("plan_slug")
    merge.add_argument("--run-id", required=True)
    controller = subcommands.add_parser("controller", help=argparse.SUPPRESS)
    controller.add_argument("--run-dir", required=True, type=Path)
    return parser


def controller_command(run_dir: Path) -> str:
    return " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(Path(__file__).resolve())),
            "controller",
            "--run-dir",
            shlex.quote(str(run_dir.resolve())),
        ]
    )


def start(plan_slug: str, max_workers: int, worker_command: str = "codex") -> str:
    if max_workers < 1:
        raise TaskGraphRuntimeError("max_workers must be at least 1")
    repository = _repository_root()
    plan_dir = repository / ".agent" / plan_slug
    if not plan_dir.is_dir():
        raise TaskGraphRuntimeError(f"plan directory does not exist: {plan_dir}")
    ensure_clean_base(repository, plan_slug)
    run_id = _run_id()
    run_dir = plan_dir / "runs" / run_id
    git = TaskGraphGit(repository)
    try:
        git_common_dir = git.common_dir()
        base_branch = git.current_branch(repository)
    except TaskGraphGitError as exc:
        raise TaskGraphRuntimeError(
            f"cannot resolve shared Git metadata directory or current branch before startup: {exc}"
        ) from exc
    base_commit = git.head_sha(repository)
    snapshot = create_run_snapshot(plan_dir, run_dir)
    feature_branch = f"task-graph/{plan_slug}/{run_id}/feature"
    integration = run_dir / "integration"
    session = f"task-graph-{plan_slug}-{run_id}"
    state = create_state(
        run_id=run_id,
        plan_slug=plan_slug,
        repository=str(repository),
        feature_branch=feature_branch,
        base_commit=base_commit,
        snapshot_digest=snapshot.dag_digest,
        task_digests=snapshot.task_digests,
        max_workers=max_workers,
        task_ids=[task["id"] for task in snapshot.dag["tasks"]],
        git_common_dir=str(git_common_dir),
        worker_command=worker_command,
        base_branch=base_branch,
    )
    state["planDirectory"] = str(plan_dir)
    state["integrationWorktree"] = str(integration)
    state["session"] = session

    with RunLock(run_dir):
        git.create_branch(feature_branch, base_commit)
        git.add_worktree(integration, feature_branch)
        write_state(run_dir, state)
        tmux = TmuxClient()
        pane_id = tmux.create_session(session, repository, controller_command(run_dir))
        pane = tmux.pane_info(pane_id)
        state["controller"] = {
            "attemptToken": uuid.uuid4().hex,
            "paneId": pane_id,
            "pid": pane.pid if pane else None,
            "startedAt": time.time(),
        }
        write_state(run_dir, state)
    return _attach_command(session)


def resume(plan_slug: str, run_id: str) -> str:
    repository = _repository_root()
    run_dir = repository / ".agent" / plan_slug / "runs" / run_id
    if not run_dir.is_dir():
        raise TaskGraphRuntimeError(f"run does not exist: {run_dir}")
    state = load_state(run_dir)
    _validate_persisted_git_common_dir(state)
    try:
        with RunLock(run_dir):
            return _resume_locked(run_dir)
    except TaskGraphRuntimeError:
        tmux = TmuxClient()
        controller = state.get("controller", {})
        pane_id, pid = controller.get("paneId"), controller.get("pid")
        if pane_id and isinstance(pid, int) and tmux.pane_is_live(pane_id, pid):
            return _attach_command(state["session"])
        raise


def status(plan_slug: str, run_id: str | None = None) -> str:
    """Report the newest (or explicitly selected) persisted run."""
    repository = _repository_root()
    run_dir = _run_directory(repository, plan_slug, run_id)
    state = load_state(run_dir)
    result = f"{state['runId']}: {_run_status(state)}"
    notification = state.get("notification")
    if not isinstance(notification, dict):
        return result
    outcome = notification.get("outcome")
    if not isinstance(outcome, str):
        return result
    error = notification.get("error")
    detail = f" ({error})" if isinstance(error, str) and error else ""
    return f"{result}; notification: {outcome}{detail}"


def merge(plan_slug: str, run_id: str) -> str:
    """Promote a completed run feature branch into its recorded base branch."""
    repository = _repository_root()
    run_dir = _run_directory(repository, plan_slug, run_id)
    with RunLock(run_dir):
        state = load_state(run_dir)
        if state.get("planSlug") != plan_slug or state.get("runId") != run_id:
            raise TaskGraphRuntimeError("run state does not match the requested plan and run ID")
        run_status = _run_status(state)
        if run_status == "already merged":
            return f"{run_id}: already merged"
        if run_status != "succeeded":
            raise TaskGraphRuntimeError("cannot merge until all tasks are integrated")
        base_branch = state.get("baseBranch")
        if not isinstance(base_branch, str) or not base_branch:
            raise TaskGraphRuntimeError(
                "run state lacks baseBranch; start a fresh run from a clean base"
            )
        feature_branch = state.get("featureBranch")
        if not isinstance(feature_branch, str) or not feature_branch:
            raise TaskGraphRuntimeError("run state lacks a feature branch")
        git = TaskGraphGit(repository)
        try:
            current_branch = git.current_branch(repository)
            if current_branch != base_branch:
                raise TaskGraphRuntimeError(
                    f"checked out branch is {current_branch}; expected recorded base branch {base_branch}"
                )
            if not git.is_clean(ignored_prefix=f".agent/{plan_slug}/runs/"):
                raise TaskGraphRuntimeError(
                    "repository is dirty outside controller-owned run artifacts"
                )
            if not git.branch_exists(feature_branch):
                raise TaskGraphRuntimeError(f"feature branch does not exist: {feature_branch}")
            result = git.merge_feature_branch(
                repository,
                feature_branch,
                f"Task Graph {plan_slug} run {run_id}",
            )
        except TaskGraphGitError as exc:
            raise TaskGraphRuntimeError(f"cannot merge Task Graph run: {exc}") from exc
        if result.outcome == "already_merged":
            return f"{run_id}: already merged"
        if result.outcome == "conflict_aborted":
            return f"{run_id}: merge conflict aborted; target branch unchanged"
        if result.outcome != "merged" or not result.merge_sha:
            raise TaskGraphRuntimeError("Git did not return a successful merge result")
        state["promotion"] = {
            "targetBranch": base_branch,
            "mergeSha": result.merge_sha,
            "mergedAt": time.time(),
        }
        write_state(run_dir, state)
    return f"{run_id}: merged into {base_branch} ({result.merge_sha})"


def run_controller(run_dir: Path) -> None:
    """Long-lived tmux service loop. The lock prevents duplicate schedulers."""
    with RunLock(run_dir, blocking=True):
        dashboard = TerminalDashboard(sys.stdout)
        controller = TaskGraphController(run_dir, event_sink=dashboard.record_event)
        try:
            dashboard.start(controller.state, controller.tasks)
            while not controller.is_complete():
                controller.run_once()
                dashboard.redraw(controller.state, controller.tasks)
                if not controller.is_complete():
                    time.sleep(1)
            dashboard.finish(controller.state, controller.tasks, _run_summary(controller.state))
        finally:
            dashboard.cleanup()
        _notify_run_completion(run_dir, controller.state)


def _run_summary(state: dict[str, object]) -> str:
    tasks = state["tasks"]
    assert isinstance(tasks, dict)
    counts = {status: sum(item["status"] == status for item in tasks.values()) for status in ("integrated", "failed", "blocked")}
    return f"run complete: {counts['integrated']} integrated, {counts['failed']} failed, {counts['blocked']} blocked"


def _run_directory(repository: Path, plan_slug: str, run_id: str | None) -> Path:
    runs_dir = repository / ".agent" / plan_slug / "runs"
    if not runs_dir.is_dir():
        raise TaskGraphRuntimeError(f"plan has no runs: {plan_slug}")
    if run_id is None:
        runs = sorted(path for path in runs_dir.iterdir() if path.is_dir())
        if not runs:
            raise TaskGraphRuntimeError(f"plan has no runs: {plan_slug}")
        return runs[-1]
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        raise TaskGraphRuntimeError(f"run does not exist: {run_dir}")
    return run_dir


def _run_status(state: dict[str, object]) -> str:
    if state.get("promotion"):
        return "already merged"
    tasks = state.get("tasks")
    if not isinstance(tasks, dict):
        raise TaskGraphRuntimeError("run state has invalid tasks")
    statuses = [task.get("status") for task in tasks.values() if isinstance(task, dict)]
    if len(statuses) != len(tasks):
        raise TaskGraphRuntimeError("run state has invalid tasks")
    if all(status == "integrated" for status in statuses):
        return "succeeded"
    if any(status in {"failed", "blocked"} for status in statuses):
        return "failed"
    return "running"


def _notify_run_completion(run_dir: Path, state: dict[str, object]) -> None:
    """Deliver and persist one completion notification while the run is locked."""
    if "notification" in state:
        return
    run_status = _run_status(state)
    if run_status not in {"succeeded", "failed"}:
        return
    plan_slug = str(state.get("planSlug", "<plan-slug>"))
    run_id = str(state.get("runId", "<run-id>"))
    command = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(Path(__file__).resolve())),
            "merge" if run_status == "succeeded" else "status",
            shlex.quote(plan_slug),
            "--run-id",
            shlex.quote(run_id),
        ]
    )
    attempted_at = time.time()
    if run_status == "succeeded":
        outcome = notify_completion(
            succeeded=True, message=f"Run {run_id} succeeded. Merge it with: {command}"
        )
    else:
        outcome = notify_completion(
            succeeded=False, message=f"Run {run_id} failed. Check it with: {command}"
        )
    notification: dict[str, object] = {
        "completionStatus": run_status,
        "attemptedAt": attempted_at,
        "outcome": outcome["outcome"],
    }
    if "error" in outcome:
        notification["error"] = outcome["error"]
    state["notification"] = notification
    write_state(run_dir, state)


def _resume_locked(run_dir: Path) -> str:
    state = load_state(run_dir)
    _validate_persisted_git_common_dir(state)
    tmux = TmuxClient()
    controller = state.get("controller", {})
    pane_id, pid = controller.get("paneId"), controller.get("pid")
    if pane_id and isinstance(pid, int) and tmux.pane_is_live(pane_id, pid):
        return _attach_command(state["session"])
    command = controller_command(run_dir)
    if tmux.session_exists(state["session"]):
        pane_id = tmux.create_window(state["session"], "controller-recovery", Path(state["repository"]), command)
    else:
        pane_id = tmux.create_session(state["session"], Path(state["repository"]), command)
    pane = tmux.pane_info(pane_id)
    state["controller"] = {
        "attemptToken": uuid.uuid4().hex,
        "paneId": pane_id,
        "pid": pane.pid if pane else None,
        "startedAt": time.time(),
    }
    write_state(run_dir, state)
    return _attach_command(state["session"])


def _repository_root() -> Path:
    try:
        return TaskGraphGit.repository_root()
    except TaskGraphGitError as exc:
        raise TaskGraphRuntimeError(str(exc)) from exc


def _validate_persisted_git_common_dir(state: dict[str, object]) -> Path:
    persisted = require_git_common_dir(state)
    try:
        resolved = TaskGraphGit(Path(str(state["repository"]))).common_dir()
    except (KeyError, TaskGraphGitError) as exc:
        raise TaskGraphRuntimeError(
            "cannot validate shared Git metadata directory; start a fresh run from a clean base"
        ) from exc
    if persisted != resolved:
        raise TaskGraphRuntimeError(
            "run state gitCommonDir does not match the repository; "
            "start a fresh run from a clean base"
        )
    return persisted


def _run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _attach_command(session: str) -> str:
    return f"tmux attach-session -t {shlex.quote(session)}"


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.action == "start":
            print(start(args.plan_slug, args.max_workers, args.worker_command))
        elif args.action == "resume":
            print(resume(args.plan_slug, args.run_id))
        elif args.action == "status":
            print(status(args.plan_slug, args.run_id))
        elif args.action == "merge":
            print(merge(args.plan_slug, args.run_id))
        else:
            run_controller(args.run_dir)
    except TaskGraphRuntimeError as exc:
        print(f"task-graph: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
