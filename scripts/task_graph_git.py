"""Git and worktree operations used by the Task Graph scheduler."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class TaskGraphGitError(RuntimeError):
    """Raised when a Git operation prevents safe task scheduling."""


@dataclass(frozen=True)
class CommitInspection:
    """Result of checking the worker's task-scoped commit contract."""

    valid: bool
    commit_sha: str | None
    commit_count: int
    has_merge: bool


class TaskGraphGit:
    """Small, path-explicit wrapper around Git subprocess calls."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve()

    def head_sha(self, worktree: Path | None = None) -> str:
        return self._run(worktree or self.repository, "rev-parse", "HEAD").strip()

    def create_branch(self, branch: str, base_commit: str) -> None:
        self._run(self.repository, "branch", branch, base_commit)

    def add_worktree(self, path: Path, branch: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(self.repository, "worktree", "add", str(path), branch)

    def create_worker_worktree(self, path: Path, branch: str, launch_base_sha: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            self.repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            launch_base_sha,
        )

    def inspect_one_task_commit(self, worktree: Path, launch_base_sha: str) -> CommitInspection:
        commit_count = int(self._run(worktree, "rev-list", "--count", f"{launch_base_sha}..HEAD").strip())
        merges = self._run(worktree, "rev-list", "--merges", f"{launch_base_sha}..HEAD").strip()
        has_merge = bool(merges)
        if commit_count != 1 or has_merge:
            return CommitInspection(False, None, commit_count, has_merge)
        return CommitInspection(True, self.head_sha(worktree), commit_count, False)

    def cherry_pick(self, integration_worktree: Path, commit_sha: str) -> None:
        self._run(integration_worktree, "cherry-pick", commit_sha)

    def abort_cherry_pick(self, integration_worktree: Path) -> bool:
        result = self._run_result(integration_worktree, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD")
        if result.returncode != 0:
            return False
        self._run(integration_worktree, "cherry-pick", "--abort")
        return True

    def is_ancestor(self, commit_sha: str, worktree: Path) -> bool:
        result = self._run_result(worktree, "merge-base", "--is-ancestor", commit_sha, "HEAD")
        if result.returncode in (0, 1):
            return result.returncode == 0
        self._raise_git_error(result)
        raise AssertionError("unreachable")

    def remove_worktree(self, path: Path) -> None:
        self._run(self.repository, "worktree", "remove", "--force", str(path))

    def _run(self, cwd: Path, *args: str) -> str:
        result = self._run_result(cwd, *args)
        if result.returncode != 0:
            self._raise_git_error(result)
        return result.stdout

    def _run_result(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )

    @staticmethod
    def _raise_git_error(result: subprocess.CompletedProcess[str]) -> None:
        details = result.stderr.strip() or result.stdout.strip() or "Git command failed"
        raise TaskGraphGitError(details)
