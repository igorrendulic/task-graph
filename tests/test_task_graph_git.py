import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_git import (
    TaskGraphGit,
    TaskGraphGitError,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(root: Path) -> None:
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "baseline.txt").write_text("baseline")
    _git(root, "add", "baseline.txt")
    _git(root, "commit", "--quiet", "-m", "baseline")


class TaskGraphGitTests(unittest.TestCase):
    def test_worker_commit_is_one_non_merge_commit_from_launch_base(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            base = git.head_sha(root)
            feature = "task-graph/demo/run-1/feature"
            integration = root / ".agent" / "demo" / "runs" / "run-1" / "integration"
            git.create_branch(feature, base)
            git.add_worktree(integration, feature)
            worker = root / ".agent" / "demo" / "runs" / "run-1" / "worktrees" / "001-first-attempt-1"
            git.create_worker_worktree(
                worker,
                "task-graph/demo/run-1/worker/001-first/attempt-1",
                git.head_sha(integration),
            )
            (worker / "feature.txt").write_text("worker change")
            _git(worker, "add", "feature.txt")
            _git(worker, "commit", "--quiet", "-m", "task: first")

            inspection = git.inspect_one_task_commit(worker, base)

            self.assertTrue(inspection.valid)
            self.assertEqual(1, inspection.commit_count)
            self.assertFalse(inspection.has_merge)
            self.assertIsNotNone(inspection.commit_sha)

    def test_integration_cherry_pick_makes_worker_commit_ancestral_to_feature_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            base = git.head_sha(root)
            feature = "task-graph/demo/run-1/feature"
            integration = root / "integration"
            worker = root / "worker"
            git.create_branch(feature, base)
            git.add_worktree(integration, feature)
            git.create_worker_worktree(worker, "task-graph/demo/run-1/worker/first", base)
            (worker / "feature.txt").write_text("worker change")
            _git(worker, "add", "feature.txt")
            _git(worker, "commit", "--quiet", "-m", "task: first")
            commit = git.inspect_one_task_commit(worker, base).commit_sha

            git.cherry_pick(integration, commit)

            self.assertTrue(git.is_ancestor(commit, integration))

    def test_multiple_commits_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            base = git.head_sha(root)
            worker = root / "worker"
            git.create_worker_worktree(worker, "task-graph/demo/run-1/worker/first", base)
            for number in (1, 2):
                (worker / f"change-{number}.txt").write_text(str(number))
                _git(worker, "add", ".")
                _git(worker, "commit", "--quiet", "-m", f"change {number}")

            inspection = git.inspect_one_task_commit(worker, base)

            self.assertFalse(inspection.valid)
            self.assertEqual(2, inspection.commit_count)
