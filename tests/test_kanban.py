import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "kanban.py"


def write_task(
    repo: Path,
    plan: str,
    column: str,
    name: str,
    title: str,
    deps: str = "None",
    task_type: str = "ship",
) -> None:
    path = repo / ".agent" / plan / column / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# {title}",
                "",
                "## Type",
                task_type,
                "",
                "## Goal",
                "Test task.",
                "",
                "## Dependencies",
                deps,
                "",
                "## Parallel",
                "Yes.",
                "",
            ]
        ),
        encoding="utf-8",
    )


class KanbanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.plan = "first-plan"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_helper(self, *args: str, plan: str | None = None) -> subprocess.CompletedProcess[str]:
        plan = self.plan if plan is None else plan
        return subprocess.run(
            [sys.executable, str(HELPER), *args, "--repo", str(self.repo), "--plan", plan],
            check=True,
            text=True,
            capture_output=True,
        )

    def init_git_repo(self) -> tuple[str, str]:
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo, check=True)
        source = self.repo / "payload.txt"
        source.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "payload.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, text=True, capture_output=True
        ).stdout.strip()
        source.write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "change", "-q"], cwd=self.repo, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, text=True, capture_output=True
        ).stdout.strip()
        return base, head

    def test_plan_json_reports_launch_batch_and_task_types(self) -> None:
        write_task(self.repo, self.plan, "todo", "001-ship.md", "Ship Task")
        write_task(self.repo, self.plan, "todo", "002-scout.md", "Scout Task", task_type="scout")
        write_task(self.repo, self.plan, "todo", "003-blocked.md", "Blocked Task", deps="Depends on: 001-ship.md.")

        result = self.run_helper("plan", "--limit", "2", "--json")

        data = json.loads(result.stdout)
        self.assertEqual(["001-ship.md", "002-scout.md"], [task["file"] for task in data["recommended_launch_batch"]])
        self.assertEqual("ship", data["recommended_launch_batch"][0]["type"])
        self.assertEqual("scout", data["recommended_launch_batch"][1]["type"])
        self.assertEqual(["003-blocked.md"], [task["file"] for task in data["sequential_or_blocked_tasks"]])

    def test_reserve_moves_launch_batch_and_initializes_run_ledger(self) -> None:
        write_task(self.repo, self.plan, "todo", "001-one.md", "One")
        write_task(self.repo, self.plan, "todo", "002-two.md", "Two")

        result = self.run_helper("reserve", "--limit", "2", "--run-id", "run-a")

        self.assertIn("Reserved launch batch (limit 2):", result.stdout)
        self.assertTrue((self.repo / ".agent" / self.plan / "in-progress" / "001-one.md").exists())
        self.assertTrue((self.repo / ".agent" / self.plan / "in-progress" / "002-two.md").exists())
        run_dir = self.repo / ".agent" / self.plan / "runs" / "run-a"
        self.assertTrue((run_dir / "briefs").is_dir())
        self.assertTrue((run_dir / "reports").is_dir())
        self.assertTrue((run_dir / "reviews").is_dir())
        ledger = (run_dir / "progress.md").read_text(encoding="utf-8")
        self.assertIn("- 001-one.md: in-progress", ledger)
        self.assertIn("- 002-two.md: in-progress", ledger)

    def test_reserve_does_not_relaunch_tasks_marked_complete_in_ledger(self) -> None:
        write_task(self.repo, self.plan, "todo", "001-done-in-ledger.md", "Already Done")
        write_task(self.repo, self.plan, "todo", "002-next.md", "Next")
        run_dir = self.repo / ".agent" / self.plan / "runs" / "run-b"
        run_dir.mkdir(parents=True)
        (run_dir / "progress.md").write_text(
            "# Task Graph Run run-b\n\n- 001-done-in-ledger.md: complete (commits abc..def, review clean)\n",
            encoding="utf-8",
        )

        self.run_helper("reserve", "--limit", "2", "--run-id", "run-b")

        self.assertTrue((self.repo / ".agent" / self.plan / "todo" / "001-done-in-ledger.md").exists())
        self.assertTrue((self.repo / ".agent" / self.plan / "in-progress" / "002-next.md").exists())

    def test_archive_diff_writes_patch_and_metadata_for_in_progress_task(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        base, head = self.init_git_repo()

        result = self.run_helper(
            "archive-diff",
            "--run-id",
            "run-a",
            "--task",
            "001-work.md",
            "--base",
            base,
            "--head",
            head,
            "--branch",
            "task-graph/first-plan/001-work",
            "--review",
            "reviews/001-work.md",
        )

        diff_dir = self.repo / ".agent" / self.plan / "runs" / "run-a" / "diffs"
        patch = diff_dir / "001-work.patch"
        metadata = diff_dir / "001-work.md"
        self.assertIn(str(patch), result.stdout)
        self.assertIn("-before", patch.read_text(encoding="utf-8"))
        summary = metadata.read_text(encoding="utf-8")
        self.assertIn("task-graph/first-plan/001-work", summary)
        self.assertIn(base, summary)
        self.assertIn(head, summary)
        self.assertIn("reviews/001-work.md", summary)
        self.assertIn("Review status: `pending`", summary)

    def test_archive_diff_rejects_unknown_revisions_without_artifacts(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        self.init_git_repo()

        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "archive-diff",
                "--repo",
                str(self.repo),
                "--plan",
                self.plan,
                "--run-id",
                "run-a",
                "--task",
                "001-work.md",
                "--base",
                "does-not-exist",
                "--head",
                "HEAD",
                "--branch",
                "task-graph/first-plan/001-work",
                "--review",
                "reviews/001-work.md",
            ],
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unknown revision", result.stderr)
        self.assertFalse((self.repo / ".agent" / self.plan / "runs" / "run-a" / "diffs").exists())

    def test_plans_with_matching_task_names_and_run_ids_are_isolated(self) -> None:
        second_plan = "second-plan"
        write_task(self.repo, self.plan, "todo", "001-work.md", "First Work")
        write_task(self.repo, second_plan, "todo", "001-work.md", "Second Work")

        self.run_helper("reserve", "--limit", "1", "--run-id", "shared-run")
        self.run_helper("reserve", "--limit", "1", "--run-id", "shared-run", plan=second_plan)
        self.run_helper("board")
        self.run_helper("board", plan=second_plan)

        first_board = (self.repo / ".agent" / self.plan / "kanban.md").read_text(encoding="utf-8")
        second_board = (self.repo / ".agent" / second_plan / "kanban.md").read_text(encoding="utf-8")
        self.assertIn("First Work", first_board)
        self.assertNotIn("Second Work", first_board)
        self.assertIn("Second Work", second_board)
        self.assertNotIn("First Work", second_board)
        self.assertTrue((self.repo / ".agent" / self.plan / "runs" / "shared-run" / "progress.md").exists())
        self.assertTrue((self.repo / ".agent" / second_plan / "runs" / "shared-run" / "progress.md").exists())

    def test_plan_argument_is_required_and_legacy_state_is_ignored(self) -> None:
        legacy_task = self.repo / ".agent" / "tasks" / "todo" / "001-legacy.md"
        legacy_task.parent.mkdir(parents=True)
        legacy_task.write_text("# Legacy Task\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(HELPER), "plan", "--repo", str(self.repo)],
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--plan", result.stderr)
        self.assertTrue(legacy_task.exists())

    def test_plan_argument_requires_a_lowercase_kebab_case_slug(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "board",
                "--repo",
                str(self.repo),
                "--plan",
                "Invalid Plan",
            ],
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("lowercase kebab-case", result.stderr)


if __name__ == "__main__":
    unittest.main()
