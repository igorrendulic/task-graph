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


@dataclass(frozen=True)
class MergeResult:
    """Outcome of promoting a Task Graph feature branch."""

    outcome: str
    merge_sha: str | None = None


class TaskGraphGit:
    """Small, path-explicit wrapper around Git subprocess calls."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve()

    @staticmethod
    def repository_root(cwd: Path | None = None) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            TaskGraphGit._raise_git_error(result)
        return Path(result.stdout.strip()).resolve()

    def head_sha(self, worktree: Path | None = None) -> str:
        return self._run(worktree or self.repository, "rev-parse", "HEAD").strip()

    def current_branch(self, worktree: Path | None = None) -> str:
        branch = self._run(worktree or self.repository, "branch", "--show-current").strip()
        if not branch:
            raise TaskGraphGitError("a branch must be checked out (detached HEAD is not supported)")
        return branch

    def branch_exists(self, branch: str) -> bool:
        result = self._run_result(
            self.repository, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
        self._raise_git_error(result)
        raise AssertionError("unreachable")

    def is_clean(
        self, worktree: Path | None = None, *, ignored_prefix: str | None = None
    ) -> bool:
        output = self._run(
            worktree or self.repository, "status", "--porcelain", "--untracked-files=all"
        )
        if not ignored_prefix:
            return not output.strip()
        return all(
            self._is_ignored_runtime_path(line, ignored_prefix)
            for line in output.splitlines()
        )

    def common_dir(self) -> Path:
        """Return the absolute shared Git metadata directory for this repository."""
        raw_path = self._run(
            self.repository,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ).strip()
        common_dir = Path(raw_path)
        if not raw_path or not common_dir.is_absolute() or not common_dir.is_dir():
            raise TaskGraphGitError(
                "Git did not return an existing absolute shared metadata directory"
            )
        return common_dir.resolve()

    def create_branch(self, branch: str, base_commit: str) -> None:
        self._run(self.repository, "branch", branch, base_commit)

    def add_worktree(self, path: Path, branch: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(self.repository, "worktree", "add", str(path), branch)

    def switch_branch(
        self, worktree: Path, branch: str, *, ignore_other_worktrees: bool = False
    ) -> None:
        """Switch an existing worktree to an existing local branch."""
        args = ["switch"]
        if ignore_other_worktrees:
            args.append("--ignore-other-worktrees")
        args.append(branch)
        self._run(worktree, *args)

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
        self._run(
            integration_worktree,
            "cherry-pick",
            "--ff",
            "--allow-empty",
            "--empty=keep",
            commit_sha,
        )

    def abort_cherry_pick(self, integration_worktree: Path) -> bool:
        result = self._run_result(integration_worktree, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD")
        if result.returncode != 0:
            return False
        self._run(integration_worktree, "cherry-pick", "--abort")
        return True

    def merge_feature_branch(
        self, target_worktree: Path, feature_branch: str, message: str
    ) -> MergeResult:
        """Merge a run feature branch, safely aborting any merge conflict."""
        if not self.branch_exists(feature_branch):
            raise TaskGraphGitError(f"feature branch does not exist: {feature_branch}")
        if self.is_ancestor(feature_branch, target_worktree):
            return MergeResult("already_merged")
        result = self._run_result(
            target_worktree,
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            message,
            feature_branch,
        )
        if result.returncode == 0:
            return MergeResult("merged", self.head_sha(target_worktree))
        if self.abort_merge(target_worktree):
            return MergeResult("conflict_aborted")
        self._raise_git_error(result)
        raise AssertionError("unreachable")

    def abort_merge(self, worktree: Path) -> bool:
        result = self._run_result(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if result.returncode != 0:
            return False
        self._run(worktree, "merge", "--abort")
        return True

    def is_ancestor(self, commit_sha: str, worktree: Path) -> bool:
        result = self._run_result(worktree, "merge-base", "--is-ancestor", commit_sha, "HEAD")
        if result.returncode in (0, 1):
            return result.returncode == 0
        self._raise_git_error(result)
        raise AssertionError("unreachable")

    def remove_worktree(self, path: Path) -> None:
        self._run(self.repository, "worktree", "remove", "--force", str(path))

    def remove_worktree_safely(self, path: Path) -> None:
        """Remove a clean worktree without discarding any local changes."""
        self._run(self.repository, "worktree", "remove", str(path))

    @staticmethod
    def _is_ignored_runtime_path(status_line: str, ignored_prefix: str) -> bool:
        path = status_line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        return path.replace("\\", "/").startswith(ignored_prefix)

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
