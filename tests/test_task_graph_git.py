import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_git import (
    MergeResult,
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
    def test_current_branch_and_branch_existence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)

            self.assertEqual(_git(root, "branch", "--show-current"), git.current_branch())
            self.assertTrue(git.branch_exists(git.current_branch()))
            self.assertFalse(git.branch_exists("does-not-exist"))

    def test_clean_check_ignores_controller_run_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            runtime_file = root / ".agent" / "demo" / "runs" / "run-1" / "state.json"
            runtime_file.parent.mkdir(parents=True)
            runtime_file.write_text("{}")

            self.assertTrue(git.is_clean(ignored_prefix=".agent/demo/runs/"))
            (root / "unrelated.txt").write_text("dirty")
            self.assertFalse(git.is_clean(ignored_prefix=".agent/demo/runs/"))

    def test_merge_feature_branch_creates_a_no_ff_merge_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            feature = "task-graph/demo/run-1/feature"
            feature_worktree = root.parent / "feature"
            git.create_branch(feature, git.head_sha())
            git.add_worktree(feature_worktree, feature)
            (feature_worktree / "feature.txt").write_text("feature")
            _git(feature_worktree, "add", "feature.txt")
            _git(feature_worktree, "commit", "--quiet", "-m", "feature")

            result = git.merge_feature_branch(root, feature, "Task Graph demo run run-1")

            self.assertEqual("merged", result.outcome)
            self.assertEqual(git.head_sha(), result.merge_sha)
            self.assertEqual(2, len(_git(root, "show", "-s", "--format=%P", "HEAD").split()))

    def test_merge_feature_branch_reports_already_merged_without_second_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            feature = "task-graph/demo/run-1/feature"
            feature_worktree = root.parent / "feature"
            git.create_branch(feature, git.head_sha())
            git.add_worktree(feature_worktree, feature)
            (feature_worktree / "feature.txt").write_text("feature")
            _git(feature_worktree, "add", "feature.txt")
            _git(feature_worktree, "commit", "--quiet", "-m", "feature")
            git.merge_feature_branch(root, feature, "Task Graph demo run run-1")
            head_before = git.head_sha()

            result = git.merge_feature_branch(root, feature, "Task Graph demo run run-1")

            self.assertEqual(MergeResult("already_merged"), result)
            self.assertEqual(head_before, git.head_sha())

    def test_conflicting_merge_is_aborted_and_leaves_target_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            feature = "task-graph/demo/run-1/feature"
            feature_worktree = root.parent / "feature"
            git.create_branch(feature, git.head_sha())
            git.add_worktree(feature_worktree, feature)
            (feature_worktree / "baseline.txt").write_text("feature")
            _git(feature_worktree, "add", "baseline.txt")
            _git(feature_worktree, "commit", "--quiet", "-m", "feature")
            (root / "baseline.txt").write_text("base")
            _git(root, "add", "baseline.txt")
            _git(root, "commit", "--quiet", "-m", "base")
            target_head = git.head_sha()

            result = git.merge_feature_branch(root, feature, "Task Graph demo run run-1")

            self.assertEqual("conflict_aborted", result.outcome)
            self.assertEqual(target_head, git.head_sha())
            self.assertTrue(git.is_clean())

    def test_common_dir_returns_owning_repository_metadata_from_linked_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            linked = root.parent / "linked"
            _git(root, "worktree", "add", "--quiet", "-b", "linked", str(linked))

            self.assertEqual((root / ".git").resolve(), TaskGraphGit(linked).common_dir())

    def test_safe_worktree_removal_releases_branch_for_primary_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            feature = "task-graph/demo/run-1/feature"
            integration = root / ".agent" / "demo" / "runs" / "run-1" / "integration"
            git.create_branch(feature, git.head_sha())
            git.add_worktree(integration, feature)

            with self.assertRaises(TaskGraphGitError):
                git.switch_branch(root, feature)

            git.remove_worktree_safely(integration)
            git.switch_branch(root, feature)

            self.assertEqual(feature, git.current_branch(root))

    def test_safe_worktree_removal_refuses_local_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            _repo(root)
            git = TaskGraphGit(root)
            integration = root / "integration"
            feature = "task-graph/demo/run-1/feature"
            git.create_branch(feature, git.head_sha())
            git.add_worktree(integration, feature)
            (integration / "uncommitted.txt").write_text("preserve me")

            with self.assertRaises(TaskGraphGitError):
                git.remove_worktree_safely(integration)

            self.assertTrue((integration / "uncommitted.txt").is_file())

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
