import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "kanban.py"
SPEC = importlib.util.spec_from_file_location("kanban", HELPER)
assert SPEC and SPEC.loader
KANBAN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = KANBAN
SPEC.loader.exec_module(KANBAN)


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

        result = self.run_helper(
            "reserve", "--limit", "2", "--run-id", "run-a", "--delivery-mode", "direct-pr"
        )

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

    def test_reserve_requires_and_persists_delivery_policy(self) -> None:
        write_task(self.repo, self.plan, "todo", "001-work.md", "Work")

        with self.assertRaisesRegex(SystemExit, "--delivery-mode"):
            KANBAN.command_reserve(self.repo, self.plan, 1, "run-a", None, False)

        KANBAN.command_reserve(self.repo, self.plan, 1, "run-a", "direct-pr", True)

        policy = json.loads(
            (self.repo / ".agent" / self.plan / "runs" / "run-a" / "policy.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual({"mode": "direct-pr", "yolo": True}, policy)

    def test_reserve_rejects_unknown_delivery_policy(self) -> None:
        with self.assertRaisesRegex(SystemExit, "delivery mode"):
            KANBAN.validate_run_policy("merge-everything", False)

    def test_reserve_does_not_relaunch_tasks_marked_complete_in_ledger(self) -> None:
        write_task(self.repo, self.plan, "todo", "001-done-in-ledger.md", "Already Done")
        write_task(self.repo, self.plan, "todo", "002-next.md", "Next")
        run_dir = self.repo / ".agent" / self.plan / "runs" / "run-b"
        run_dir.mkdir(parents=True)
        (run_dir / "progress.md").write_text(
            "# Task Graph Run run-b\n\n- 001-done-in-ledger.md: complete (commits abc..def, review clean)\n",
            encoding="utf-8",
        )

        self.run_helper("reserve", "--limit", "2", "--run-id", "run-b", "--delivery-mode", "direct-pr")

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

        self.run_helper("reserve", "--limit", "1", "--run-id", "shared-run", "--delivery-mode", "direct-pr")
        self.run_helper(
            "reserve", "--limit", "1", "--run-id", "shared-run", "--delivery-mode", "direct-pr", plan=second_plan
        )
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

    def test_runtime_record_uses_a_deterministic_tmux_session_and_validates(self) -> None:
        task = KANBAN.Task("in-progress", Path("001-work.md"), "Work", (), "ship")
        now = "2026-07-14T12:00:00+00:00"

        record = KANBAN.new_runtime_record(
            task=task,
            plan="first-plan",
            run_id="run-a",
            branch="task-graph/first-plan/001-work",
            worktree=Path("/tmp/worktree"),
            brief=Path("/tmp/brief.md"),
            report=Path("/tmp/report.md"),
            log=Path("/tmp/task.log"),
            command=["codex", "exec"],
            started_at=now,
            base_commit="a" * 40,
        )

        self.assertEqual("task-graph-first-plan-run-a-001-work", record["session"])
        self.assertIsNone(record["pid"])
        self.assertTrue(KANBAN.is_valid_runtime_record(record))
        record.pop("log")
        self.assertFalse(KANBAN.is_valid_runtime_record(record))

    def test_runtime_record_includes_verified_base_commit(self) -> None:
        task = KANBAN.Task("in-progress", Path("001-work.md"), "Work", (), "ship")

        record = KANBAN.new_runtime_record(
            task=task,
            plan="first-plan",
            run_id="run-a",
            branch="task-graph/first-plan/001-work",
            worktree=Path("/tmp/worktree"),
            brief=Path("/tmp/brief.md"),
            report=Path("/tmp/report.md"),
            log=Path("/tmp/task.log"),
            command=["codex", "exec"],
            base_commit="a" * 40,
        )

        self.assertEqual("a" * 40, record["base_commit"])
        self.assertTrue(KANBAN.is_valid_runtime_record(record))

    def test_launch_exec_refuses_before_writing_when_tmux_is_unavailable(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        with patch.object(KANBAN.shutil, "which", return_value=None):
            with self.assertRaisesRegex(SystemExit, "tmux is required"):
                KANBAN.command_launch_exec(
                    self.repo,
                    self.plan,
                    "run-a",
                    "001-work.md",
                    "branch",
                    Path("/tmp/worktree"),
                )
        self.assertFalse((self.repo / ".agent" / self.plan / "runs" / "run-a" / "runtime").exists())

    def test_launch_exec_rejects_controller_checkout_before_runtime_write(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        with patch.object(KANBAN.shutil, "which", return_value="/usr/bin/tmux"):
            with self.assertRaisesRegex(SystemExit, "controller checkout"):
                KANBAN.command_launch_exec(
                    self.repo, self.plan, "run-a", "001-work.md", "main", self.repo
                )
        self.assertFalse((self.repo / ".agent" / self.plan / "runs" / "run-a" / "runtime").exists())

    def test_launch_exec_captures_the_final_report_without_granting_controller_access(self) -> None:
        task_name = "001-work.md"
        run_id = "run-a"
        write_task(self.repo, self.plan, "in-progress", task_name, "Work")
        brief = self.repo / ".agent" / self.plan / "runs" / run_id / "briefs" / task_name
        brief.parent.mkdir(parents=True)
        brief.write_text("# Brief\n", encoding="utf-8")

        with (
            patch.object(KANBAN.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.object(KANBAN, "verified_worktree", return_value=(Path("/tmp/worktree"), "a" * 40)),
            patch.object(
                KANBAN.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 0),
                    subprocess.CompletedProcess([], 0),
                    subprocess.CompletedProcess([], 0, stdout="123\n"),
                ],
            ) as run,
        ):
            KANBAN.command_launch_exec(
                self.repo, self.plan, run_id, task_name, "branch", Path("/tmp/worktree")
            )

        runtime = self.repo / ".agent" / self.plan / "runs" / run_id / "runtime" / "001-work.json"
        record = json.loads(runtime.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                "codex",
                "exec",
                "--sandbox",
                "workspace-write",
                "--output-last-message",
                str(self.repo / ".agent" / self.plan / "runs" / run_id / "reports" / task_name),
            ],
            record["command"],
        )
        wrapper = run.call_args_list[0].args[0][-1]
        self.assertNotIn("--ask-for-approval", wrapper)
        self.assertNotIn("--add-dir", wrapper)
        self.assertIn("Return the complete final report", wrapper)
        self.assertNotIn("write the final report", wrapper)
        self.assertIn("do not run git commit", wrapper)

    def write_runtime(self, run_id: str, task_name: str, **overrides: object) -> Path:
        directory = self.repo / ".agent" / self.plan / "runs" / run_id / "runtime"
        directory.mkdir(parents=True, exist_ok=True)
        started = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        record: dict[str, object] = {
            "version": 1,
            "plan": self.plan,
            "run_id": run_id,
            "task": task_name,
            "session": KANBAN.tmux_session_name(self.plan, run_id, task_name),
            "pid": 123,
            "command": ["codex", "exec"],
            "branch": "branch",
            "worktree": "/tmp/worktree",
            "base_commit": "a" * 40,
            "brief": f"briefs/{task_name}",
            "report": f"reports/{task_name}",
            "log": f"logs/{Path(task_name).stem}.log",
            "started_at": started,
            "finished_at": None,
            "exit_code": None,
        }
        record.update(overrides)
        path = directory / f"{Path(task_name).stem}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_status_classifies_runtime_records_and_legacy_runs(self) -> None:
        for name in ("001-running.md", "002-success.md", "003-failure.md", "004-stale.md", "005-legacy.md"):
            write_task(self.repo, self.plan, "in-progress", name, name)
        self.write_runtime("run-a", "001-running.md")
        self.write_runtime("run-a", "002-success.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        self.write_runtime("run-a", "003-failure.md", finished_at=datetime.now(UTC).isoformat(), exit_code=2)
        self.write_runtime("run-a", "004-stale.md", started_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
        (self.repo / ".agent" / self.plan / "runs" / "run-a" / "reports").mkdir(parents=True)
        (self.repo / ".agent" / self.plan / "runs" / "run-a" / "reports" / "002-success.md").write_text("DONE\n", encoding="utf-8")

        def tmux_alive(session: str) -> str:
            return "RUNNING" if session.endswith("001-running") else "IDLE_OR_DEAD"

        entries = KANBAN.collect_status(self.repo, tmux_alive=tmux_alive, stale_after=timedelta(minutes=30))
        states = {entry["task"]: entry["state"] for entry in entries}

        self.assertEqual("RUNNING", states["001-running.md"])
        self.assertEqual("SUCCEEDED_AWAITING_REVIEW", states["002-success.md"])
        self.assertEqual("NEEDS_ATTENTION", states["003-failure.md"])
        self.assertEqual("STALE", states["004-stale.md"])
        self.assertEqual("UNKNOWN", states["005-legacy.md"])
        success = next(entry for entry in entries if entry["task"] == "002-success.md")
        self.assertIn("report", success["recovery_hint"])

    def test_tmux_liveness_distinguishes_harness_shell_and_unknown(self) -> None:
        with patch.object(KANBAN.subprocess, "run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout="codex\n"),
            ]
            self.assertEqual("RUNNING", KANBAN.tmux_liveness("worker"))

        with patch.object(KANBAN.subprocess, "run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout="zsh\n"),
            ]
            self.assertEqual("IDLE_OR_DEAD", KANBAN.tmux_liveness("worker"))

        with patch.object(KANBAN.subprocess, "run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout="python\n"),
            ]
            self.assertEqual("UNKNOWN", KANBAN.tmux_liveness("worker"))

    def test_status_filters_and_json_are_read_only(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        self.write_runtime("run-a", "001-work.md", finished_at=datetime.now(UTC).isoformat(), exit_code=1)
        before = sorted(path.relative_to(self.repo) for path in self.repo.rglob("*"))

        result = self.run_helper("status", "--run-id", "run-a", "--task", "001-work.md", "--json")
        payload = json.loads(result.stdout)

        self.assertEqual(1, len(payload["tasks"]))
        self.assertEqual("001-work.md", payload["tasks"][0]["task"])
        self.assertEqual(before, sorted(path.relative_to(self.repo) for path in self.repo.rglob("*")))

    def test_delivery_readiness_requires_green_evidence_even_when_yolo(self) -> None:
        run = self.repo / ".agent" / self.plan / "runs" / "run-a"
        run.mkdir(parents=True)
        (run / "policy.json").write_text(
            json.dumps({"mode": "direct-pr", "yolo": True}), encoding="utf-8"
        )

        with self.assertRaisesRegex(SystemExit, "verified review and tests"):
            KANBAN.command_delivery_ready(self.repo, self.plan, "run-a", "001-work.md")

        self.write_runtime("run-a", "001-work.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        report = run / "reports" / "001-work.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("DONE\nTests: passed\n", encoding="utf-8")
        review = run / "reviews" / "001-work.md"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("Review status: approved\n", encoding="utf-8")

        self.assertEqual("MERGE_GREEN_PR", KANBAN.command_delivery_ready(self.repo, self.plan, "run-a", "001-work.md"))

    def test_teardown_refuses_unlanded_work_without_explicit_discard(self) -> None:
        self.write_runtime("run-a", "001-work.md")

        with self.assertRaisesRegex(SystemExit, "unlanded work"):
            KANBAN.command_teardown(self.repo, self.plan, "run-a", "001-work.md", discard=False)


if __name__ == "__main__":
    unittest.main()
