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
from scripts.task_graph_workspace import WorkspaceError, load_workspace


DEFAULT_MAX_WORKERS = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute a Task Graph DAG")
    subcommands = parser.add_subparsers(dest="action", required=True)
    start = subcommands.add_parser("start", help="start a new plan run")
    start.add_argument("plan_slug")
    start.add_argument("--workspace", type=Path, help="workspace root containing .agent/task-graph.workspace.json")
    start.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    start.add_argument(
        "--worker-command",
        default="codex",
        help="worker executable recorded in run state (default: codex)",
    )
    resume = subcommands.add_parser("resume", help="reconnect or restart a plan controller")
    resume.add_argument("plan_slug")
    resume.add_argument("run_id")
    resume.add_argument("--workspace", type=Path)
    status = subcommands.add_parser("status", help="report the state of a plan run")
    status.add_argument("plan_slug")
    status.add_argument("--run-id")
    status.add_argument("--workspace", type=Path)
    merge = subcommands.add_parser("merge", help="promote a successful plan run")
    merge.add_argument("plan_slug")
    merge.add_argument("--run-id", required=True)
    merge.add_argument("--workspace", type=Path)
    merge.add_argument("--project", help="declared workspace project ID to promote")
    checkout = subcommands.add_parser(
        "checkout", help="check out a successful plan run for local inspection"
    )
    checkout.add_argument("plan_slug")
    checkout.add_argument("--run-id", required=True)
    checkout.add_argument("--workspace", type=Path)
    checkout.add_argument("--project", help="declared workspace project ID to inspect")
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


def start(
    plan_slug: str, max_workers: int, worker_command: str = "codex", workspace: Path | None = None
) -> str:
    if max_workers < 1:
        raise TaskGraphRuntimeError("max_workers must be at least 1")
    if workspace is not None:
        return _workspace_start(plan_slug, max_workers, worker_command, workspace)
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


def resume(plan_slug: str, run_id: str, workspace: Path | None = None) -> str:
    if workspace is not None:
        return _workspace_resume(plan_slug, run_id, workspace)
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


def status(plan_slug: str, run_id: str | None = None, workspace: Path | None = None) -> str:
    """Report the newest (or explicitly selected) persisted run."""
    if workspace is not None:
        return _workspace_status(plan_slug, run_id, workspace)
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


def _cleanup_integration_worktree(git: TaskGraphGit, run_dir: Path) -> str:
    integration = run_dir / "integration"
    if not integration.exists():
        return ""
    if not git.is_clean(integration):
        return "; integration worktree is dirty; retained"
    if input("Remove the clean integration worktree? [y/N] ").strip().lower() != "y":
        return "; integration worktree retained"
    git.remove_worktree_safely(integration)
    return "; integration worktree removed"


def merge(
    plan_slug: str, run_id: str, workspace: Path | None = None, project: str | None = None
) -> str:
    """Promote a completed run feature branch into its recorded base branch."""
    if workspace is not None:
        return _workspace_merge(plan_slug, run_id, workspace, project)
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
        try:
            cleanup = _cleanup_integration_worktree(git, run_dir)
        except TaskGraphGitError as exc:
            raise TaskGraphRuntimeError(f"cannot merge Task Graph run: {exc}") from exc
    return f"{run_id}: merged into {base_branch} ({result.merge_sha}){cleanup}"


def checkout(
    plan_slug: str, run_id: str, workspace: Path | None = None, project: str | None = None
) -> str:
    """Switch the primary checkout to a succeeded run's feature branch."""
    if workspace is not None:
        return _workspace_checkout(plan_slug, run_id, workspace, project)
    repository = _repository_root()
    run_dir = _run_directory(repository, plan_slug, run_id)
    with RunLock(run_dir):
        state = load_state(run_dir)
        if state.get("planSlug") != plan_slug or state.get("runId") != run_id:
            raise TaskGraphRuntimeError("run state does not match the requested plan and run ID")
        run_status = _run_status(state)
        if run_status == "already merged":
            raise TaskGraphRuntimeError("cannot check out a run that is already merged")
        if run_status != "succeeded":
            raise TaskGraphRuntimeError("only succeeded runs can be checked out")
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
            if not git.is_clean(ignored_prefix=f".agent/{plan_slug}/runs/"):
                raise TaskGraphRuntimeError(
                    "repository is dirty outside controller-owned run artifacts"
                )
            if not git.branch_exists(feature_branch):
                raise TaskGraphRuntimeError(f"feature branch does not exist: {feature_branch}")
            git.switch_branch(repository, feature_branch, ignore_other_worktrees=True)
        except TaskGraphGitError as exc:
            raise TaskGraphRuntimeError(f"cannot check out Task Graph run: {exc}") from exc
    switch_back = " ".join(["git", "switch", shlex.quote(base_branch)])
    merge_command = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(Path(__file__).resolve())),
            "merge",
            shlex.quote(plan_slug),
            "--run-id",
            shlex.quote(run_id),
        ]
    )
    return (
        f"{run_id}: checked out {feature_branch}. "
        f"Return to {base_branch}: {switch_back}. Then merge: {merge_command}"
    )


def _workspace_context(path: Path):
    try:
        return load_workspace(path)
    except WorkspaceError as exc:
        raise TaskGraphRuntimeError(str(exc)) from exc


def _workspace_run_directory(workspace: Path, plan_slug: str, run_id: str | None) -> Path:
    return _run_directory(workspace, plan_slug, run_id)


def _workspace_start(plan_slug: str, max_workers: int, worker_command: str, path: Path) -> str:
    workspace = _workspace_context(path)
    plan_dir = workspace.root / ".agent" / plan_slug
    if not plan_dir.is_dir():
        raise TaskGraphRuntimeError(f"plan directory does not exist: {plan_dir}")
    for project in workspace.projects:
        ensure_clean_base(project.path, plan_slug)
    run_id = _run_id()
    run_dir = plan_dir / "runs" / run_id
    snapshot = create_run_snapshot(
        plan_dir, run_dir, workspace_project_ids=set(workspace.by_id)
    )
    projects: dict[str, dict[str, object]] = {}
    for project in workspace.projects:
        git = TaskGraphGit(project.path)
        try:
            common_dir, base_branch = git.common_dir(), git.current_branch(project.path)
            base_commit = git.head_sha(project.path)
        except TaskGraphGitError as exc:
            raise TaskGraphRuntimeError(f"cannot prepare workspace project {project.id}: {exc}") from exc
        projects[project.id] = {
            "repository": str(project.path), "gitCommonDir": str(common_dir),
            "baseBranch": base_branch, "baseCommit": base_commit,
            "featureBranch": f"task-graph/{plan_slug}/{run_id}/{project.id}/feature",
            "integrationWorktree": str(run_dir / "projects" / project.id / "integration"),
            "used": True,
        }
    state = {
        "schemaVersion": 1, "workspace": str(workspace.root), "runId": run_id,
        "planSlug": plan_slug, "planDirectory": str(plan_dir), "projects": projects,
        "dagDigest": snapshot.dag_digest, "taskDigests": snapshot.task_digests,
        "maxWorkers": max_workers, "workerCommand": worker_command, "createdAt": time.time(),
        "controller": {}, "session": f"task-graph-{plan_slug}-{run_id}",
        "tasks": {task["id"]: {"status": "pending", "attempts": [], "commitSha": None} for task in snapshot.dag["tasks"]},
    }
    with RunLock(run_dir):
        for project_id, info in projects.items():
            git = TaskGraphGit(Path(str(info["repository"])))
            git.create_branch(str(info["featureBranch"]), str(info["baseCommit"]))
            git.add_worktree(Path(str(info["integrationWorktree"])), str(info["featureBranch"]))
        write_state(run_dir, state)
        tmux = TmuxClient()
        pane_id = tmux.create_session(state["session"], workspace.root, controller_command(run_dir))
        pane = tmux.pane_info(pane_id)
        state["controller"] = {"attemptToken": uuid.uuid4().hex, "paneId": pane_id, "pid": pane.pid if pane else None, "startedAt": time.time()}
        write_state(run_dir, state)
    return _attach_command(state["session"])


def _workspace_resume(plan_slug: str, run_id: str, path: Path) -> str:
    workspace = _workspace_context(path)
    run_dir = _workspace_run_directory(workspace.root, plan_slug, run_id)
    with RunLock(run_dir):
        state = load_state(run_dir)
        _validate_workspace_projects(state)
        return _resume_locked(run_dir)


def _workspace_status(plan_slug: str, run_id: str | None, path: Path) -> str:
    workspace = _workspace_context(path)
    state = load_state(_workspace_run_directory(workspace.root, plan_slug, run_id))
    projects = state.get("projects", {})
    detail = "; ".join(
        f"{project_id}: {project.get('featureBranch')} ({project.get('integrationWorktree')}); "
        f"promotion={'merged' if project.get('promotion') else 'pending'}"
        for project_id, project in projects.items() if project.get("used")
    )
    return f"{state['runId']}: {_run_status(state)}" + (f"; {detail}" if detail else "")


def _workspace_project(state: dict[str, object], project_id: str | None) -> tuple[str, dict[str, object]]:
    projects = state.get("projects")
    if not isinstance(projects, dict):
        raise TaskGraphRuntimeError("run is not a workspace run")
    if not project_id:
        raise TaskGraphRuntimeError("--project is required to promote or check out a workspace project")
    project = projects.get(project_id)
    if not isinstance(project, dict) or not project.get("used"):
        raise TaskGraphRuntimeError(f"unknown or unused workspace project: {project_id}")
    return project_id, project


def _workspace_merge(plan_slug: str, run_id: str, path: Path, project_id: str | None) -> str:
    workspace = _workspace_context(path)
    run_dir = _workspace_run_directory(workspace.root, plan_slug, run_id)
    with RunLock(run_dir):
        state = load_state(run_dir)
        _validate_workspace_projects(state)
        project_id, project = _workspace_project(state, project_id)
        if _run_status(state) != "succeeded":
            raise TaskGraphRuntimeError("cannot merge until all tasks are integrated")
        if project.get("promotion"):
            return f"{run_id}: {project_id} already merged"
        repository = Path(str(project["repository"]))
        git = TaskGraphGit(repository)
        try:
            if git.current_branch(repository) != project["baseBranch"]:
                raise TaskGraphRuntimeError(f"checked out branch is not recorded base branch {project['baseBranch']}")
            if not git.is_clean():
                raise TaskGraphRuntimeError(f"workspace project {project_id} is dirty")
            result = git.merge_feature_branch(repository, str(project["featureBranch"]), f"Task Graph {plan_slug} run {run_id} ({project_id})")
        except TaskGraphGitError as exc:
            raise TaskGraphRuntimeError(f"cannot merge workspace project {project_id}: {exc}") from exc
        if result.outcome == "conflict_aborted":
            return f"{run_id}: {project_id} merge conflict aborted; target branch unchanged"
        if result.outcome == "already_merged":
            return f"{run_id}: {project_id} already merged"
        project["promotion"] = {"targetBranch": project["baseBranch"], "mergeSha": result.merge_sha, "mergedAt": time.time()}
        write_state(run_dir, state)
        return f"{run_id}: {project_id} merged into {project['baseBranch']} ({result.merge_sha})"


def _workspace_checkout(plan_slug: str, run_id: str, path: Path, project_id: str | None) -> str:
    workspace = _workspace_context(path)
    run_dir = _workspace_run_directory(workspace.root, plan_slug, run_id)
    with RunLock(run_dir):
        state = load_state(run_dir)
        _validate_workspace_projects(state)
        project_id, project = _workspace_project(state, project_id)
        if _run_status(state) != "succeeded":
            raise TaskGraphRuntimeError("only succeeded runs can be checked out")
        if project.get("promotion"):
            raise TaskGraphRuntimeError(f"workspace project {project_id} is already merged")
        repository = Path(str(project["repository"]))
        git = TaskGraphGit(repository)
        try:
            if not git.is_clean():
                raise TaskGraphRuntimeError(f"workspace project {project_id} is dirty")
            git.switch_branch(repository, str(project["featureBranch"]), ignore_other_worktrees=True)
        except TaskGraphGitError as exc:
            raise TaskGraphRuntimeError(f"cannot check out workspace project {project_id}: {exc}") from exc
    return f"{run_id}: checked out {project['featureBranch']} in {project_id}. Return to {project['baseBranch']} before merging."


def _validate_workspace_projects(state: dict[str, object]) -> None:
    projects = state.get("projects")
    if not isinstance(projects, dict):
        raise TaskGraphRuntimeError("run is not a workspace run")
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            raise TaskGraphRuntimeError("workspace run state has invalid projects")
        try:
            git = TaskGraphGit(Path(str(project["repository"])))
            if Path(str(project["gitCommonDir"])).resolve() != git.common_dir():
                raise TaskGraphRuntimeError(f"workspace project {project_id} Git metadata does not match its repository")
        except (KeyError, TaskGraphGitError) as exc:
            raise TaskGraphRuntimeError(f"cannot validate workspace project {project_id}") from exc


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
    workspace = state.get("workspace")
    workspace_args = (
        ["--workspace", shlex.quote(str(workspace))]
        if isinstance(workspace, str) and workspace
        else []
    )
    if workspace_args:
        command = " ".join(
            [shlex.quote(sys.executable), shlex.quote(str(Path(__file__).resolve())), "status", shlex.quote(plan_slug), "--run-id", shlex.quote(run_id), *workspace_args]
        )
        attempted_at = time.time()
        outcome = notify_completion(
            succeeded=run_status == "succeeded",
            message=(f"Workspace run {run_id} {run_status}. Inspect project branches and worktrees with: {command}"),
        )
        state["notification"] = {"completionStatus": run_status, "attemptedAt": attempted_at, "outcome": outcome["outcome"], **({"error": outcome["error"]} if "error" in outcome else {})}
        write_state(run_dir, state)
        return
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
        checkout_command = " ".join(
            [
                shlex.quote(sys.executable),
                shlex.quote(str(Path(__file__).resolve())),
                "checkout",
                shlex.quote(plan_slug),
                "--run-id",
                shlex.quote(run_id),
            ]
        )
        outcome = notify_completion(
            succeeded=True,
            message=(
                f"Run {run_id} succeeded. Inspect it with: {checkout_command}. "
                f"After switching back to the recorded base branch, merge it with: {command}"
            ),
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
        controller_cwd = Path(str(state.get("repository") or state.get("workspace")))
        pane_id = tmux.create_window(state["session"], "controller-recovery", controller_cwd, command)
    else:
        controller_cwd = Path(str(state.get("repository") or state.get("workspace")))
        pane_id = tmux.create_session(state["session"], controller_cwd, command)
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
            print(start(args.plan_slug, args.max_workers, args.worker_command, args.workspace))
        elif args.action == "resume":
            print(resume(args.plan_slug, args.run_id, args.workspace))
        elif args.action == "status":
            print(status(args.plan_slug, args.run_id, args.workspace))
        elif args.action == "merge":
            print(merge(args.plan_slug, args.run_id, args.workspace, args.project))
        elif args.action == "checkout":
            print(checkout(args.plan_slug, args.run_id, args.workspace, args.project))
        else:
            run_controller(args.run_dir)
    except TaskGraphRuntimeError as exc:
        print(f"task-graph: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
