import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "kanban.py"


def write_task(repo: Path, column: str, name: str, title: str, deps: str = "None", task_type: str = "ship") -> None:
    path = repo / ".agent" / "tasks" / column / name
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
        for column in ("todo", "in-progress", "done"):
            (self.repo / ".agent" / "tasks" / column).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), *args, "--repo", str(self.repo)],
            check=True,
            text=True,
            capture_output=True,
        )

    def test_plan_json_reports_launch_batch_and_task_types(self) -> None:
        write_task(self.repo, "todo", "001-ship.md", "Ship Task")
        write_task(self.repo, "todo", "002-scout.md", "Scout Task", task_type="scout")
        write_task(self.repo, "todo", "003-blocked.md", "Blocked Task", deps="Depends on: 001-ship.md.")

        result = self.run_helper("plan", "--limit", "2", "--json")

        data = json.loads(result.stdout)
        self.assertEqual(["001-ship.md", "002-scout.md"], [task["file"] for task in data["recommended_launch_batch"]])
        self.assertEqual("ship", data["recommended_launch_batch"][0]["type"])
        self.assertEqual("scout", data["recommended_launch_batch"][1]["type"])
        self.assertEqual(["003-blocked.md"], [task["file"] for task in data["sequential_or_blocked_tasks"]])

    def test_reserve_moves_launch_batch_and_initializes_run_ledger(self) -> None:
        write_task(self.repo, "todo", "001-one.md", "One")
        write_task(self.repo, "todo", "002-two.md", "Two")

        result = self.run_helper("reserve", "--limit", "2", "--run-id", "run-a")

        self.assertIn("Reserved launch batch (limit 2):", result.stdout)
        self.assertTrue((self.repo / ".agent" / "tasks" / "in-progress" / "001-one.md").exists())
        self.assertTrue((self.repo / ".agent" / "tasks" / "in-progress" / "002-two.md").exists())
        run_dir = self.repo / ".agent" / "runs" / "run-a"
        self.assertTrue((run_dir / "briefs").is_dir())
        self.assertTrue((run_dir / "reports").is_dir())
        self.assertTrue((run_dir / "reviews").is_dir())
        ledger = (run_dir / "progress.md").read_text(encoding="utf-8")
        self.assertIn("- 001-one.md: in-progress", ledger)
        self.assertIn("- 002-two.md: in-progress", ledger)

    def test_reserve_does_not_relaunch_tasks_marked_complete_in_ledger(self) -> None:
        write_task(self.repo, "todo", "001-done-in-ledger.md", "Already Done")
        write_task(self.repo, "todo", "002-next.md", "Next")
        run_dir = self.repo / ".agent" / "runs" / "run-b"
        run_dir.mkdir(parents=True)
        (run_dir / "progress.md").write_text(
            "# Task Graph Run run-b\n\n- 001-done-in-ledger.md: complete (commits abc..def, review clean)\n",
            encoding="utf-8",
        )

        self.run_helper("reserve", "--limit", "2", "--run-id", "run-b")

        self.assertTrue((self.repo / ".agent" / "tasks" / "todo" / "001-done-in-ledger.md").exists())
        self.assertTrue((self.repo / ".agent" / "tasks" / "in-progress" / "002-next.md").exists())


if __name__ == "__main__":
    unittest.main()
