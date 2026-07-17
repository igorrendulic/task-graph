import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_runtime import (
    RunLock,
    TaskGraphRuntimeError,
    create_run_snapshot,
    create_state,
    ensure_clean_base,
    load_snapshot,
    load_state,
    write_state,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_plan(root: Path) -> Path:
    plan_dir = root / ".agent" / "demo-plan"
    todo = plan_dir / "todo"
    todo.mkdir(parents=True)
    (todo / "001-first.md").write_text("# First\n\n## Dependencies\n\nNone\n")
    (plan_dir / "dag.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "planSlug": "demo-plan",
                "tasks": [
                    {
                        "id": "001-first",
                        "taskFile": "001-first.md",
                        "title": "First",
                        "instructions": "Implement the first task.",
                        "predictedPaths": ["src/first.py"],
                        "predictedSymbols": [],
                        "dependsOn": [],
                        "parallelSafe": True,
                        "schedulingRationale": "The task is isolated.",
                    }
                ],
            }
        )
    )
    return plan_dir


class TaskGraphRuntimeTests(unittest.TestCase):
    def test_state_persists_the_default_worker_command(self):
        state = create_state(
            run_id="run-1",
            plan_slug="demo-plan",
            repository="/repo",
            feature_branch="task-graph/demo-plan/run-1",
            base_commit="abc123",
            snapshot_digest="digest",
            task_digests={"001-first": "task-digest"},
            max_workers=2,
            task_ids=["001-first"],
            git_common_dir="/repo/.git",
        )

        self.assertEqual("codex", state["workerCommand"])
        self.assertEqual("/repo/.git", state["gitCommonDir"])

    def test_snapshot_uses_original_dag_and_task_content_after_plan_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_dir = _make_plan(root)
            run_dir = plan_dir / "runs" / "run-1"

            snapshot = create_run_snapshot(plan_dir, run_dir)
            (plan_dir / "dag.json").write_text("{}")
            (plan_dir / "todo" / "001-first.md").write_text("changed")

            loaded = load_snapshot(run_dir)

            self.assertEqual(snapshot.dag_digest, loaded.dag_digest)
            self.assertEqual("Implement the first task.", loaded.dag["tasks"][0]["instructions"])
            self.assertEqual(
                "# First\n\n## Dependencies\n\nNone\n",
                loaded.task_contents["001-first"],
            )

    def test_atomic_state_write_leaves_complete_json_after_a_prior_state(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            state = create_state(
                run_id="run-1",
                plan_slug="demo-plan",
                repository="/repo",
                feature_branch="task-graph/demo-plan/run-1",
                base_commit="abc123",
                snapshot_digest="digest",
                task_digests={"001-first": "task-digest"},
                max_workers=2,
                task_ids=["001-first"],
                git_common_dir="/repo/.git",
            )
            write_state(run_dir, state)
            state["tasks"]["001-first"]["status"] = "running"
            write_state(run_dir, state)

            self.assertEqual("running", load_state(run_dir)["tasks"]["001-first"]["status"])

    def test_legacy_state_without_git_common_dir_requires_a_fresh_run(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            state = create_state(
                run_id="run-1",
                plan_slug="demo-plan",
                repository="/repo",
                feature_branch="task-graph/demo-plan/run-1",
                base_commit="abc123",
                snapshot_digest="digest",
                task_digests={"001-first": "task-digest"},
                max_workers=2,
                task_ids=["001-first"],
                git_common_dir="/repo/.git",
            )
            del state["gitCommonDir"]
            write_state(run_dir, state)

            with self.assertRaisesRegex(TaskGraphRuntimeError, "start a fresh run from a clean base"):
                load_state(run_dir)

    def test_second_lock_holder_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            with RunLock(run_dir):
                with self.assertRaises(TaskGraphRuntimeError):
                    with RunLock(run_dir):
                        pass

    def test_dirty_tracked_or_untracked_files_reject_a_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _git(root, "init", "--quiet")
            _git(root, "config", "user.name", "Test")
            _git(root, "config", "user.email", "test@example.invalid")
            (root / "tracked.txt").write_text("baseline")
            _git(root, "add", "tracked.txt")
            _git(root, "commit", "--quiet", "-m", "baseline")
            (root / "tracked.txt").write_text("changed")

            with self.assertRaisesRegex(TaskGraphRuntimeError, "dirty"):
                ensure_clean_base(root, "demo-plan")

            _git(root, "checkout", "--", "tracked.txt")
            (root / "untracked.txt").write_text("changed")
            with self.assertRaisesRegex(TaskGraphRuntimeError, "dirty"):
                ensure_clean_base(root, "demo-plan")
