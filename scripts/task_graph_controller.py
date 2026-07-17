"""Dependency scheduler coordinating immutable Task Graph worker attempts."""

from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from scripts.task_graph_board import move_task, render_kanban
from scripts.task_graph_git import TaskGraphGit, TaskGraphGitError
from scripts.task_graph_runtime import (
    TaskGraphRuntimeError,
    load_snapshot,
    load_state,
    require_git_common_dir,
    write_state,
)
from scripts.task_graph_tmux import TmuxClient


class TaskGraphController:
    """One run-scoped scheduler; state, Git, and tmux stay behind adapters."""

    def __init__(
        self,
        run_dir: Path,
        *,
        git: TaskGraphGit | None = None,
        tmux: TmuxClient | None = None,
        codex_bin: str | None = None,
        event_sink: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.snapshot = load_snapshot(self.run_dir)
        self.state = load_state(self.run_dir)
        self.tasks = {task["id"]: task for task in self.snapshot.dag["tasks"]}
        self.git = git or TaskGraphGit(Path(self.state["repository"]))
        self.git_common_dir = require_git_common_dir(self.state)
        try:
            resolved_common_dir = self.git.common_dir()
        except TaskGraphGitError as exc:
            raise TaskGraphRuntimeError(
                "cannot validate shared Git metadata directory; "
                "start a fresh run from a clean base"
            ) from exc
        if self.git_common_dir != resolved_common_dir:
            raise TaskGraphRuntimeError(
                "run state gitCommonDir does not match the repository; "
                "start a fresh run from a clean base"
            )
        self.tmux = tmux or TmuxClient()
        self.codex_bin = codex_bin or self.state.get("workerCommand", "codex")
        self.integration_worktree = Path(self.state["integrationWorktree"])
        self.event_sink = event_sink

    def build_worker_prompt(self, task_id: str, repair_context: str | None = None) -> str:
        """Build a self-contained prompt without relying on `.agent` in a worktree."""
        task = self.tasks[task_id]
        dependency_lines = [
            f"- {dependency}: {self.state['tasks'][dependency]['commitSha']}"
            for dependency in task["dependsOn"]
        ]
        dependencies = "\n".join(dependency_lines) or "- None"
        repair = repair_context or "None"
        return f"""You are the isolated worker for Task Graph task {task_id}.

Implement only this task. Run the focused tests described below and create one
non-merge, task-scoped Git commit. Do not cherry-pick, merge, rebase, or modify
the Task Graph controller/runtime artifacts.

## Task brief

{self.snapshot.task_contents[task_id]}

## Integrated prerequisite commits

{dependencies}

## Repair context

{repair}
"""

    def schedule_ready_tasks(self) -> None:
        """Launch ready work up to the run's fixed concurrency limit."""
        active = sum(
            task["status"] == "running" for task in self.state["tasks"].values()
        )
        capacity = self.state["maxWorkers"] - active
        for task_id in sorted(self.tasks):
            if capacity <= 0:
                break
            if self._is_ready(task_id):
                self._launch_attempt(task_id)
                capacity -= 1

    def run_once(self) -> None:
        """Reconcile persistence, inspect exits, integrate commits, then schedule."""
        self.reconcile()
        self.poll_running_attempts()
        self.integrate_waiting_tasks()
        self.schedule_ready_tasks()

    def reconcile(self) -> None:
        """Recover an interrupted integration without trusting stale status alone."""
        self.git.abort_cherry_pick(self.integration_worktree)
        for task_id, task_state in self.state["tasks"].items():
            if task_state["status"] != "integrating":
                continue
            commit = task_state.get("commitSha")
            if commit and self.git.is_ancestor(commit, self.integration_worktree):
                self._transition(task_id, "integrated", "integration")
                task_state["integratedAt"] = time.time()
            else:
                self._transition(task_id, "awaiting_integration", "worker_exit")
        write_state(self.run_dir, self.state)

    def poll_running_attempts(self) -> None:
        """Read worker completion sentinels and enforce the commit contract."""
        for task_id, task_state in self.state["tasks"].items():
            if task_state["status"] != "running":
                continue
            attempt = task_state["attempts"][-1]
            exit_file = Path(attempt["exitFile"])
            if not exit_file.is_file():
                pane_id = attempt.get("paneId")
                pid = attempt.get("pid")
                if pane_id and isinstance(pid, int) and not self.tmux.pane_is_live(pane_id, pid):
                    self._record_failure(task_id, "worker pane exited without completion sentinel")
                    write_state(self.run_dir, self.state)
                continue
            try:
                exit_code = int(exit_file.read_text(encoding="utf-8").strip())
            except ValueError:
                self._record_failure(task_id, "worker completion sentinel is invalid")
                continue
            attempt["exitCode"] = exit_code
            attempt["endedAt"] = time.time()
            inspection = self.git.inspect_one_task_commit(
                Path(attempt["worktree"]), attempt["launchBaseSha"]
            )
            if exit_code == 0 and inspection.valid and inspection.commit_sha:
                task_state["commitSha"] = inspection.commit_sha
                task_state["status"] = "awaiting_integration"
            else:
                reason = f"worker exit {exit_code}; commits={inspection.commit_count}; merge={inspection.has_merge}"
                self._record_failure(task_id, reason)
            write_state(self.run_dir, self.state)

    def integrate_waiting_tasks(self) -> None:
        """Cherry-pick completed worker commits in a persistent state transition."""
        for task_id in sorted(self.tasks):
            task_state = self.state["tasks"][task_id]
            if task_state["status"] != "awaiting_integration":
                continue
            commit = task_state["commitSha"]
            self._transition(task_id, "integrating")
            write_state(self.run_dir, self.state)
            try:
                self.git.cherry_pick(self.integration_worktree, commit)
            except TaskGraphGitError as exc:
                self.git.abort_cherry_pick(self.integration_worktree)
                self._record_failure(task_id, f"cherry-pick failed: {exc}")
                write_state(self.run_dir, self.state)
                continue
            self._transition(task_id, "integrated", "integration")
            task_state["integratedAt"] = time.time()
            attempt = task_state["attempts"][-1]
            try:
                self.git.remove_worktree(Path(attempt["worktree"]))
            except TaskGraphGitError:
                attempt["cleanupFailed"] = True
            self._update_board(task_id, "done")
            write_state(self.run_dir, self.state)

    def is_complete(self) -> bool:
        return all(
            task["status"] in {"integrated", "failed", "blocked"}
            for task in self.state["tasks"].values()
        )

    def _is_ready(self, task_id: str) -> bool:
        task_state = self.state["tasks"][task_id]
        if task_state["status"] not in {"pending", "retrying"}:
            return False
        return all(
            self.state["tasks"][dependency]["status"] == "integrated"
            for dependency in self.tasks[task_id]["dependsOn"]
        )

    def _launch_attempt(self, task_id: str) -> None:
        task_state = self.state["tasks"][task_id]
        attempt_number = len(task_state["attempts"]) + 1
        launch_base = self.git.head_sha(self.integration_worktree)
        worktree = self.run_dir / "worktrees" / f"{task_id}-attempt-{attempt_number}"
        branch = (
            f"task-graph/{self.state['planSlug']}/{self.state['runId']}"
            f"/worker/{task_id}/attempt-{attempt_number}"
        )
        logs = self.run_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        prefix = logs / f"{task_id}-attempt-{attempt_number}"
        attempt: dict[str, Any] = {
            "number": attempt_number,
            "branch": branch,
            "worktree": str(worktree),
            "launchBaseSha": launch_base,
            "startedAt": time.time(),
            "stdoutLog": str(prefix.with_suffix(".stdout")),
            "stderrLog": str(prefix.with_suffix(".stderr")),
            "combinedLog": str(prefix.with_suffix(".log")),
            "exitFile": str(prefix.with_suffix(".exit")),
            "attemptToken": f"{task_id}-{attempt_number}-{time.time_ns()}",
            "paneId": None,
            "pid": None,
        }
        task_state["attempts"].append(attempt)
        self._transition(task_id, "running", "launch")
        self._update_board(task_id, "in-progress")
        write_state(self.run_dir, self.state)

        self.git.create_worker_worktree(worktree, branch, launch_base)
        repair_context = task_state["attempts"][-2].get("failureSummary") if attempt_number > 1 else None
        command = self._worker_command(worktree, task_id, attempt, repair_context)
        pane_id = self.tmux.create_window(
            self.state["session"], task_id, worktree, command
        )
        pane = self.tmux.pane_info(pane_id)
        attempt["paneId"] = pane_id
        attempt["pid"] = pane.pid if pane else None
        write_state(self.run_dir, self.state)

    def _worker_command(
        self,
        worktree: Path,
        task_id: str,
        attempt: dict[str, Any],
        repair_context: str | None,
    ) -> str:
        stdout = Path(attempt["stdoutLog"])
        stderr = Path(attempt["stderrLog"])
        combined = Path(attempt["combinedLog"])
        exit_file = Path(attempt["exitFile"])
        formatter = Path(__file__).with_name("task_graph_jsonl.py")
        stream_template = combined.parent / ".task-graph-stream.XXXXXX"
        codex = " ".join(
            [
                shlex.quote(self.codex_bin),
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--add-dir",
                shlex.quote(str(self.git_common_dir)),
                "-C",
                shlex.quote(str(worktree)),
                shlex.quote(self.build_worker_prompt(task_id, repair_context)),
            ]
        )
        script = (
            f"stream_dir=$(mktemp -d {shlex.quote(str(stream_template))}) || exit 1; "
            "stdout_pipe=\"$stream_dir/stdout\"; stderr_pipe=\"$stream_dir/stderr\"; "
            "combined_pipe=\"$stream_dir/combined\"; "
            "trap 'rm -f -- \"$stdout_pipe\" \"$stderr_pipe\" \"$combined_pipe\"; rmdir \"$stream_dir\"' EXIT; "
            "mkfifo \"$stdout_pipe\" \"$stderr_pipe\" \"$combined_pipe\" || exit 1; "
            f"cat <\"$combined_pipe\" >{shlex.quote(str(combined))} & combined_pid=$!; "
            f"tee {shlex.quote(str(stdout))} <\"$stdout_pipe\" "
            "| tee \"$combined_pipe\" "
            f"| {shlex.quote(sys.executable)} {shlex.quote(str(formatter))} & stdout_pid=$!; "
            f"tee {shlex.quote(str(stderr))} <\"$stderr_pipe\" "
            "| tee \"$combined_pipe\" >&2 & stderr_pid=$!; "
            "PYTHONDONTWRITEBYTECODE=1 "
            'PYTEST_ADDOPTS="${PYTEST_ADDOPTS:+${PYTEST_ADDOPTS} }-p no:cacheprovider" '
            f"{codex} >\"$stdout_pipe\" 2>\"$stderr_pipe\"; code=$?; "
            "wait \"$stdout_pid\"; wait \"$stderr_pid\"; wait \"$combined_pid\"; "
            f"printf '%s\\n' \"$code\" >{shlex.quote(str(exit_file))}; exit \"$code\""
        )
        return f"bash -o pipefail -c {shlex.quote(script)}"

    def _record_failure(self, task_id: str, summary: str) -> None:
        task_state = self.state["tasks"][task_id]
        task_state["attempts"][-1]["failureSummary"] = summary
        if len(task_state["attempts"]) < 2:
            self._transition(task_id, "retrying", "retry", summary)
            return
        self._transition(task_id, "failed", "failure", summary)
        self._block_descendants(task_id)

    def _block_descendants(self, failed_task_id: str) -> None:
        changed = True
        while changed:
            changed = False
            for task_id, task in self.tasks.items():
                task_state = self.state["tasks"][task_id]
                if task_state["status"] not in {"pending", "retrying"}:
                    continue
                if any(
                    self.state["tasks"][dependency]["status"] in {"failed", "blocked"}
                    for dependency in task["dependsOn"]
                ):
                    self._transition(task_id, "blocked", "block", failed_task_id)
                    task_state["blockedBy"] = failed_task_id
                    changed = True

    def _transition(self, task_id: str, status: str, event_kind: str | None = None, detail: str | None = None) -> bool:
        """Change state once and publish only real lifecycle transitions."""
        task_state = self.state["tasks"][task_id]
        previous = task_state["status"]
        if previous == status:
            return False
        task_state["status"] = status
        if event_kind and self.event_sink:
            event = {"kind": event_kind, "taskId": task_id, "from": previous, "to": status}
            if detail:
                event["detail"] = detail
            self.event_sink(event)
        return True

    def _update_board(self, task_id: str, column: str) -> None:
        plan_directory = self.state.get("planDirectory")
        if not plan_directory:
            return
        plan_dir = Path(plan_directory)
        move_task(plan_dir, self.tasks[task_id]["taskFile"], column)
        render_kanban(plan_dir)
