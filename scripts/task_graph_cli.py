"""Command line entry points for Task Graph execution runs."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.task_graph_controller import TaskGraphController
from scripts.task_graph_git import TaskGraphGit
from scripts.task_graph_runtime import (
    RunLock,
    TaskGraphRuntimeError,
    create_run_snapshot,
    create_state,
    ensure_clean_base,
    load_state,
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
    controller = subcommands.add_parser("controller", help=argparse.SUPPRESS)
    controller.add_argument("--run-dir", required=True, type=Path)
    subcommands.add_parser("eval-controller", help="run opt-in controller integration evals")
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
        worker_command=worker_command,
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
    try:
        with RunLock(run_dir):
            return _resume_locked(run_dir)
    except TaskGraphRuntimeError:
        state = load_state(run_dir)
        tmux = TmuxClient()
        controller = state.get("controller", {})
        pane_id, pid = controller.get("paneId"), controller.get("pid")
        if pane_id and isinstance(pid, int) and tmux.pane_is_live(pane_id, pid):
            return _attach_command(state["session"])
        raise


def run_controller(run_dir: Path) -> None:
    """Long-lived tmux service loop. The lock prevents duplicate schedulers."""
    with RunLock(run_dir, blocking=True):
        controller = TaskGraphController(run_dir)
        while not controller.is_complete():
            controller.run_once()
            if not controller.is_complete():
                time.sleep(1)


def _resume_locked(run_dir: Path) -> str:
    state = load_state(run_dir)
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
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise TaskGraphRuntimeError(result.stderr.strip() or "not inside a Git repository")
    return Path(result.stdout.strip()).resolve()


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
        elif args.action == "eval-controller":
            from scripts.task_graph_controller_eval import run_controller_evals

            run_controller_evals()
        else:
            run_controller(args.run_dir)
    except TaskGraphRuntimeError as exc:
        print(f"task-graph: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
