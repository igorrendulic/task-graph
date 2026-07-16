import json
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "kanban.py"
WATCHER_HELPER = ROOT / "scripts" / "watcher.py"
SPEC = importlib.util.spec_from_file_location("kanban", HELPER)
assert SPEC and SPEC.loader
KANBAN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = KANBAN
SPEC.loader.exec_module(KANBAN)
WATCHER_SPEC = importlib.util.spec_from_file_location("watcher", WATCHER_HELPER)
assert WATCHER_SPEC and WATCHER_SPEC.loader
WATCHER = importlib.util.module_from_spec(WATCHER_SPEC)
sys.modules[WATCHER_SPEC.name] = WATCHER
WATCHER_SPEC.loader.exec_module(WATCHER)


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

    def test_prepare_completed_task_commits_dirty_worker_worktree(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        self.write_runtime("run-a", "001-work.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        runtime = self.repo / ".agent" / self.plan / "runs" / "run-a" / "runtime" / "001-work.json"
        record = json.loads(runtime.read_text(encoding="utf-8"))
        record.update({"worktree": "/tmp/worktree", "branch": "task-branch", "base_commit": "a" * 40})
        runtime.write_text(json.dumps(record), encoding="utf-8")

        with patch.object(KANBAN, "git_output", return_value=" M payload.txt\n"), patch.object(
            KANBAN.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run, patch.object(KANBAN, "resolved_commit", return_value="b" * 40):
            prepared = KANBAN.prepare_completed_task(self.repo, self.plan, "run-a", "001-work.md")

        self.assertEqual("b" * 40, prepared["head_commit"])
        self.assertEqual(["git", "add", "-A"], run.call_args_list[0].args[0])

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
        stale_runtime = self.write_runtime("run-a", "004-stale.md", started_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
        stale_timestamp = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
        os.utime(stale_runtime, (stale_timestamp, stale_timestamp))
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

    def test_live_worker_with_fresh_log_is_running_and_does_not_queue_inspection(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        self.write_runtime("run-a", "001-work.md")
        log = self.repo / ".agent" / self.plan / "runs" / "run-a" / "logs" / "001-work.log"
        log.parent.mkdir(parents=True)
        log.write_text("still working\n", encoding="utf-8")

        with patch.object(KANBAN, "tmux_liveness", return_value="RUNNING"):
            entries = KANBAN.collect_status(self.repo, self.plan, tmux_alive=lambda _session: "RUNNING")
            actions = KANBAN.reconcile_actions(self.repo, self.plan)
            wakes = KANBAN.supervise_once(self.repo, self.plan)

        self.assertEqual("RUNNING", entries[0]["state"])
        self.assertEqual([], actions)
        self.assertEqual([], wakes)

    def test_live_worker_with_stale_artifacts_requires_inspection(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        runtime = self.write_runtime("run-a", "001-work.md")
        log = self.repo / ".agent" / self.plan / "runs" / "run-a" / "logs" / "001-work.log"
        log.parent.mkdir(parents=True)
        log.write_text("stalled\n", encoding="utf-8")
        stale = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
        os.utime(runtime, (stale, stale))
        os.utime(log, (stale, stale))

        with patch.object(KANBAN, "tmux_liveness", return_value="RUNNING"):
            entries = KANBAN.collect_status(self.repo, self.plan, tmux_alive=lambda _session: "RUNNING")
            actions = KANBAN.reconcile_actions(self.repo, self.plan)

        self.assertEqual("STALE", entries[0]["state"])
        self.assertEqual("INSPECTION_REQUIRED", actions[0]["action"])

    def test_fresh_activity_returns_a_live_worker_from_stale_to_running(self) -> None:
        runtime = self.write_runtime("run-a", "001-work.md")
        log = self.repo / ".agent" / self.plan / "runs" / "run-a" / "logs" / "001-work.log"
        log.parent.mkdir(parents=True)
        log.write_text("stalled\n", encoding="utf-8")
        stale = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
        os.utime(runtime, (stale, stale))
        os.utime(log, (stale, stale))
        record = json.loads(runtime.read_text(encoding="utf-8"))
        run = runtime.parents[1]

        stale_entry = KANBAN.status_entry(
            plan=self.plan, run_id="run-a", task_name="001-work.md", run=run, record=record,
            tmux_alive=lambda _session: "RUNNING", stale_after=timedelta(minutes=30), now=datetime.now(UTC),
        )
        log.write_text("working again\n", encoding="utf-8")
        fresh_entry = KANBAN.status_entry(
            plan=self.plan, run_id="run-a", task_name="001-work.md", run=run, record=record,
            tmux_alive=lambda _session: "RUNNING", stale_after=timedelta(minutes=30), now=datetime.now(UTC),
        )

        self.assertEqual("STALE", stale_entry["state"])
        self.assertEqual("RUNNING", fresh_entry["state"])

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
                subprocess.CompletedProcess([], 0, stdout="bash\n"),
            ]
            self.assertEqual("UNKNOWN", KANBAN.tmux_liveness("worker"))

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

    def test_status_exposes_done_with_concerns_for_controller_recovery(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        self.write_runtime("run-a", "001-work.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        report = self.repo / ".agent" / self.plan / "runs" / "run-a" / "reports" / "001-work.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("DONE_WITH_CONCERNS\nConcern: missing edge-case coverage\n", encoding="utf-8")

        entries = KANBAN.collect_status(self.repo, tmux_alive=lambda _session: "IDLE_OR_DEAD")

        self.assertEqual(1, len(entries))
        self.assertEqual("NEEDS_ATTENTION", entries[0]["state"])
        self.assertEqual("DONE_WITH_CONCERNS", entries[0]["report_status"])
        self.assertIn("report", entries[0]["recovery_hint"])

    def test_watch_exec_signals_each_actionable_status_without_changing_artifacts(self) -> None:
        before = sorted(path.relative_to(self.repo) for path in self.repo.rglob("*"))
        for state in ("SUCCEEDED_AWAITING_REVIEW", "NEEDS_ATTENTION", "STALE", "UNKNOWN"):
            with self.subTest(state=state), patch.object(
                WATCHER.KANBAN,
                "collect_status",
                return_value=[{
                    "plan": self.plan,
                    "run_id": "run-a",
                    "task": "001-work.md",
                    "state": state,
                    "elapsed": 1,
                    "session": "worker",
                    "last_activity": None,
                    "recovery_hint": "Inspect.",
                }],
            ) as collect, patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(
                    0,
                    WATCHER.watch_exec(self.repo, self.plan, "run-a", "001-work.md", 5, checkpoint=True),
                )

            self.assertIn(f"signal: {state}", output.getvalue())
            self.assertIn("001-work.md", output.getvalue())
            collect.assert_called_once_with(self.repo, self.plan, "run-a", "001-work.md")
        self.assertEqual(before, sorted(path.relative_to(self.repo) for path in self.repo.rglob("*")))

    def test_watch_exec_times_out_while_workers_are_running(self) -> None:
        running = [{
            "plan": self.plan,
            "run_id": "run-a",
            "task": "001-work.md",
            "state": "RUNNING",
            "elapsed": 1,
            "session": "worker",
            "last_activity": None,
            "recovery_hint": "Attach.",
        }]
        with patch.object(WATCHER.KANBAN, "collect_status", return_value=running), patch.object(
            WATCHER.time, "monotonic", side_effect=(0.0, 0.0, 5.0)
        ), patch.object(WATCHER.time, "sleep") as sleep, patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            self.assertEqual(
                124,
                WATCHER.watch_exec(self.repo, self.plan, "run-a", "001-work.md", 5, checkpoint=True),
            )

        sleep.assert_called_once_with(5)
        self.assertEqual("checkpoint: no actionable wake within 5s\n", output.getvalue())

    def test_watch_exec_exits_when_no_active_workers_remain_and_honors_filters(self) -> None:
        before = sorted(path.relative_to(self.repo) for path in self.repo.rglob("*"))
        with patch.object(WATCHER.KANBAN, "collect_status", return_value=[]) as collect, patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            self.assertEqual(
                0,
                WATCHER.watch_exec(self.repo, self.plan, "run-a", "001-work.md", 5, checkpoint=True),
            )

        collect.assert_called_once_with(self.repo, self.plan, "run-a", "001-work.md")
        self.assertEqual("checkpoint: no active exec workers\n", output.getvalue())
        self.assertEqual(before, sorted(path.relative_to(self.repo) for path in self.repo.rglob("*")))

    def test_watch_exec_default_continues_after_an_actionable_status(self) -> None:
        actionable = [{
            "plan": self.plan,
            "run_id": "run-a",
            "task": "001-work.md",
            "state": "SUCCEEDED_AWAITING_REVIEW",
            "report_status": "DONE",
            "elapsed": 1,
            "session": "very-long-session-name",
            "last_activity": "2026-07-15T17:00:00+00:00",
            "recovery_hint": "Open report: /very/long/path/reports/001-work.md",
        }]
        with patch.object(WATCHER.KANBAN, "collect_status", return_value=actionable) as collect, patch.object(
            WATCHER.time, "monotonic", side_effect=(0.0, 0.0, 5.0)
        ), patch.object(WATCHER.time, "sleep") as sleep, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(124, WATCHER.watch_exec(self.repo, self.plan, None, None, 5))

        self.assertEqual(2, collect.call_count)
        sleep.assert_called_once_with(5)
        rendered = output.getvalue()
        self.assertIn("Task Graph exec monitor", rendered)
        self.assertNotIn("signal:", rendered)
        self.assertNotIn("/very/long/path", rendered)
        self.assertNotIn("very-long-session-name", rendered)

    def test_watch_exec_selects_latest_run_and_retains_running_history(self) -> None:
        entries = [
            {"plan": "plan-a", "run_id": "old", "task": "001-work.md", "state": "RUNNING", "last_activity": "2026-07-15T10:00:00+00:00"},
            {"plan": "plan-a", "run_id": "new", "task": "001-work.md", "state": "SUCCEEDED_AWAITING_REVIEW", "last_activity": "2026-07-15T11:00:00+00:00"},
            {"plan": "plan-a", "run_id": "older", "task": "002-work.md", "state": "STALE", "last_activity": "2026-07-15T09:00:00+00:00"},
            {"plan": "plan-a", "run_id": "newest", "task": "002-work.md", "state": "NEEDS_ATTENTION", "last_activity": "2026-07-15T12:00:00+00:00"},
        ]

        selected, hidden = WATCHER.select_entries(entries)

        self.assertEqual(["old", "new", "newest"], [entry["run_id"] for entry in selected])
        self.assertEqual(1, hidden)

    def test_watch_exec_rejects_nonpositive_seconds(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HELPER), "watch-exec", "--repo", str(self.repo), "--seconds", "0"],
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--seconds must be greater than zero", result.stderr)

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

    def test_record_delivery_marks_landed_work_for_teardown(self) -> None:
        self.write_runtime("run-a", "001-work.md")

        KANBAN.command_record_delivery(self.repo, self.plan, "run-a", "001-work.md", "landed")

        delivery = json.loads(
            (self.repo / ".agent" / self.plan / "runs" / "run-a" / "delivery" / "001-work.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("landed", delivery["result"])

    def test_teardown_kills_recorded_tmux_session(self) -> None:
        self.write_runtime("run-a", "001-work.md")
        KANBAN.command_record_delivery(self.repo, self.plan, "run-a", "001-work.md", "landed")

        with patch.object(KANBAN, "git_output", return_value=""), patch.object(
            KANBAN.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            KANBAN.command_teardown(self.repo, self.plan, "run-a", "001-work.md", discard=False)

        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(["git", "worktree", "remove", "/tmp/worktree"], calls)
        self.assertIn(
            ["tmux", "kill-session", "-t", "task-graph-first-plan-run-a-001-work"], calls
        )

    def test_teardown_allows_already_absent_tmux_session(self) -> None:
        self.write_runtime("run-a", "001-work.md")
        KANBAN.command_record_delivery(self.repo, self.plan, "run-a", "001-work.md", "landed")

        with patch.object(KANBAN, "git_output", return_value=""), patch.object(
            KANBAN.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 1, stderr="can't find session: worker"),
            ],
        ):
            KANBAN.command_teardown(self.repo, self.plan, "run-a", "001-work.md", discard=False)

    def test_reconcile_returns_review_for_current_success_and_hides_done_history(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "002-current.md", "Current")
        write_task(self.repo, self.plan, "done", "001-old.md", "Old")
        self.write_runtime("old-run", "001-old.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        self.write_runtime("current-run", "002-current.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        reports = self.repo / ".agent" / self.plan / "runs" / "current-run" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "002-current.md").write_text("DONE\n", encoding="utf-8")

        actions = KANBAN.reconcile_actions(self.repo, self.plan)

        self.assertEqual(["002-current.md"], [action["task"] for action in actions])
        self.assertEqual("REVIEW_REQUIRED", actions[0]["action"])

    def test_reconcile_turns_rejected_review_into_one_repair_then_checkpoint(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        self.write_runtime("run-a", "001-work.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        run = self.repo / ".agent" / self.plan / "runs" / "run-a"
        (run / "reports").mkdir(parents=True, exist_ok=True)
        (run / "reports" / "001-work.md").write_text("DONE\n", encoding="utf-8")
        review = run / "reviews" / "001-work.md"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("Review status: changes_requested\n", encoding="utf-8")

        self.assertEqual("REPAIR_REQUIRED", KANBAN.reconcile_actions(self.repo, self.plan)[0]["action"])

        KANBAN.reserve_repair_attempt(
            self.repo,
            self.plan,
            "001-work.md",
            attempt=1,
            child_run_id="run-a-task001-repair1",
            branch="task-branch-repair-1",
            worktree=Path("/tmp/repair-worktree"),
        )

        self.assertEqual("REPAIR_REQUIRED", KANBAN.reconcile_actions(self.repo, self.plan)[0]["action"])

        KANBAN.mark_repair_attempt_phase(self.repo, self.plan, "001-work.md", "failed")

        self.assertEqual("REPAIR_REQUIRED", KANBAN.reconcile_actions(self.repo, self.plan)[0]["action"])

        KANBAN.mark_repair_attempt_phase(self.repo, self.plan, "001-work.md", "launched")

        self.assertEqual("RETRY_DECISION_REQUIRED", KANBAN.reconcile_actions(self.repo, self.plan)[0]["action"])

    def test_repair_attempt_record_counts_only_launched_attempts(self) -> None:
        (self.repo / ".agent" / self.plan).mkdir(parents=True)
        record = KANBAN.reserve_repair_attempt(
            self.repo,
            self.plan,
            "001-work.md",
            attempt=1,
            child_run_id="run-a-task001-repair1",
            branch="task-branch-repair-1",
            worktree=Path("/tmp/repair-worktree"),
        )

        self.assertEqual("reserved", record["phase"])
        self.assertEqual(0, KANBAN.repair_attempts(self.repo, self.plan, "001-work.md"))

        KANBAN.mark_repair_attempt_phase(self.repo, self.plan, "001-work.md", "launched")

        self.assertEqual(1, KANBAN.repair_attempts(self.repo, self.plan, "001-work.md"))

    def test_supervise_queues_actionable_wake_before_reporting(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        self.write_runtime("run-a", "001-work.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        report = self.repo / ".agent" / self.plan / "runs" / "run-a" / "reports" / "001-work.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("DONE\n", encoding="utf-8")

        wakes = KANBAN.supervise_once(self.repo, self.plan)

        self.assertEqual("REVIEW_REQUIRED", wakes[0]["action"])
        queue = self.repo / ".agent" / self.plan / "state" / "wake-queue.jsonl"
        self.assertEqual(wakes, [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()])

        claimed = KANBAN.claim_wake(self.repo, self.plan, str(wakes[0]["id"]))
        self.assertEqual("REVIEW_REQUIRED", claimed["action"])
        KANBAN.acknowledge_wake(self.repo, self.plan, str(wakes[0]["id"]))
        with self.assertRaisesRegex(SystemExit, "already acknowledged"):
            KANBAN.claim_wake(self.repo, self.plan, str(wakes[0]["id"]))

    def test_write_atomic_syncs_file_and_parent_directory(self) -> None:
        path = self.repo / "state" / "record.json"
        events: list[tuple[str, int | None]] = []
        original_open = KANBAN.os.open
        original_close = KANBAN.os.close

        def open_directory(candidate: str | Path, flags: int, *args: object) -> int:
            if Path(candidate) == path.parent:
                return 99
            return original_open(candidate, flags, *args)

        def replace(target: Path) -> Path:
            events.append(("replace", None))
            return target

        with patch.object(KANBAN.os, "open", side_effect=open_directory), patch.object(KANBAN.os, "close", side_effect=lambda fd: None if fd == 99 else original_close(fd)), patch.object(
            KANBAN.os, "fsync", side_effect=lambda fd: events.append(("fsync", fd))
        ), patch.object(Path, "replace", side_effect=replace):
            KANBAN.write_atomic(path, "{}\n")

        replace_index = events.index(("replace", None))
        self.assertEqual("fsync", events[0][0])
        self.assertLess(0, replace_index)
        self.assertEqual(("fsync", 99), events[-1])

    def test_append_jsonl_durable_syncs_new_queue_file_and_parent_directory(self) -> None:
        (self.repo / ".agent").mkdir()
        queue = KANBAN.supervision_queue_path(self.repo, self.plan)
        queue.parent.mkdir(parents=True)
        fsync_fds: list[int] = []
        original_open = KANBAN.os.open
        original_close = KANBAN.os.close

        def open_directory(candidate: str | Path, flags: int, *args: object) -> int:
            if Path(candidate) == queue.parent:
                return 99
            return original_open(candidate, flags, *args)

        with patch.object(KANBAN.os, "open", side_effect=open_directory), patch.object(KANBAN.os, "close", side_effect=lambda fd: None if fd == 99 else original_close(fd)), patch.object(
            KANBAN.os, "fsync", side_effect=fsync_fds.append
        ):
            KANBAN.append_jsonl_durable(queue, [{"id": "wake-1", "task": "001-work.md", "action": "INSPECTION_REQUIRED"}])

        self.assertTrue(queue.exists())
        self.assertEqual(99, fsync_fds[1])

    def test_append_jsonl_durable_syncs_parent_of_new_queue_directory(self) -> None:
        plan_dir = self.repo / ".agent" / self.plan
        plan_dir.mkdir(parents=True)
        queue = KANBAN.supervision_queue_path(self.repo, self.plan)
        fsync_fds: list[int] = []
        original_open = KANBAN.os.open
        original_close = KANBAN.os.close

        def open_directory(candidate: str | Path, flags: int, *args: object) -> int:
            if Path(candidate) == queue.parent:
                return 99
            if Path(candidate) == plan_dir:
                return 100
            return original_open(candidate, flags, *args)

        with patch.object(KANBAN.os, "open", side_effect=open_directory), patch.object(
            KANBAN.os, "close", side_effect=lambda fd: None if fd in {99, 100} else original_close(fd)
        ), patch.object(KANBAN.os, "fsync", side_effect=fsync_fds.append):
            KANBAN.append_jsonl_durable(queue, [{"id": "wake-1", "task": "001-work.md", "action": "INSPECTION_REQUIRED"}])

        self.assertEqual([99, 100], fsync_fds[1:3])

    def test_supervise_syncs_parent_of_lock_created_state_directory(self) -> None:
        plan_dir = self.repo / ".agent" / self.plan
        plan_dir.mkdir(parents=True)
        action = {"task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED", "reason": "done"}
        state_dir = KANBAN.supervision_dir(self.repo, self.plan)
        fsync_fds: list[int] = []
        original_open = KANBAN.os.open
        original_close = KANBAN.os.close

        def open_directory(candidate: str | Path, flags: int, *args: object) -> int:
            if Path(candidate) == state_dir:
                return 99
            if Path(candidate) == plan_dir:
                return 100
            return original_open(candidate, flags, *args)

        with patch.object(KANBAN, "reconcile_actions", return_value=[action]), patch.object(
            KANBAN, "write_atomic"
        ), patch.object(KANBAN.os, "open", side_effect=open_directory), patch.object(
            KANBAN.os, "close", side_effect=lambda fd: None if fd in {99, 100} else original_close(fd)
        ), patch.object(KANBAN.os, "fsync", side_effect=fsync_fds.append):
            KANBAN.supervise_once(self.repo, self.plan)

        self.assertLess(fsync_fds.index(99), fsync_fds.index(100))

    def test_queue_write_failure_does_not_update_wake_index(self) -> None:
        action = {"task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED", "reason": "done"}
        (self.repo / ".agent").mkdir()

        with patch.object(KANBAN, "reconcile_actions", return_value=[action]), patch.object(
            KANBAN, "append_jsonl_durable", side_effect=OSError("disk full"), create=True
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                KANBAN.supervise_once(self.repo, self.plan)

        self.assertFalse(KANBAN.supervision_index_path(self.repo, self.plan).exists())

    def test_index_write_failure_recovers_persisted_wake_without_duplicate(self) -> None:
        action = {"task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED", "reason": "done"}
        (self.repo / ".agent").mkdir()
        index = KANBAN.supervision_index_path(self.repo, self.plan)
        original_write_atomic = KANBAN.write_atomic

        def fail_index_write(path: Path, content: str) -> None:
            if path == index:
                raise OSError("index unavailable")
            original_write_atomic(path, content)

        with patch.object(KANBAN, "reconcile_actions", return_value=[action]), patch.object(
            KANBAN, "write_atomic", side_effect=fail_index_write
        ):
            with self.assertRaisesRegex(OSError, "index unavailable"):
                KANBAN.supervise_once(self.repo, self.plan)

        queued = KANBAN.queued_wakes(self.repo, self.plan)
        self.assertEqual(1, len(queued))
        with patch.object(KANBAN, "reconcile_actions", return_value=[action]):
            self.assertEqual([], KANBAN.supervise_once(self.repo, self.plan))
        self.assertEqual(queued, KANBAN.queued_wakes(self.repo, self.plan))
        self.assertTrue(index.exists())

    def test_malformed_wake_queue_entry_is_state_corruption(self) -> None:
        (self.repo / ".agent").mkdir()
        queue = KANBAN.supervision_queue_path(self.repo, self.plan)
        queue.parent.mkdir(parents=True)
        for entry in ("   \n", '{"id": "wake-1"}\n', '{"id": "wake-1", "task": "001-work.md"}\n'):
            with self.subTest(entry=entry):
                queue.write_text(entry, encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "Malformed wake queue entry"):
                    KANBAN.queued_wakes(self.repo, self.plan)

    def test_escalated_wake_is_terminal_and_cannot_be_reclaimed(self) -> None:
        state = self.repo / ".agent" / self.plan / "state"
        state.mkdir(parents=True)
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "INSPECTION_REQUIRED"}
        (state / "wake-queue.jsonl").write_text(json.dumps(wake) + "\n", encoding="utf-8")

        KANBAN.claim_wake(self.repo, self.plan, "wake-1")
        KANBAN.escalate_wake(self.repo, self.plan, "wake-1")

        self.assertEqual("escalated", KANBAN.load_json_object(state / "wake-claims.json")["wake-1"])
        with self.assertRaisesRegex(SystemExit, "already escalated"):
            KANBAN.claim_wake(self.repo, self.plan, "wake-1")

    def test_supervision_recovers_an_append_before_index_crash_without_duplicate_wake(self) -> None:
        action = {"task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED", "reason": "done"}
        fingerprint = "001-work.md:run-a:REVIEW_REQUIRED"
        wake = {**action, "id": __import__("hashlib").sha256(fingerprint.encode()).hexdigest()[:16]}
        queue = self.repo / ".agent" / self.plan / "state" / "wake-queue.jsonl"
        queue.parent.mkdir(parents=True)
        queue.write_text(json.dumps(wake) + "\n", encoding="utf-8")

        with patch.object(KANBAN, "reconcile_actions", return_value=[action]):
            self.assertEqual([], KANBAN.supervise_once(self.repo, self.plan))

        self.assertEqual([wake], KANBAN.queued_wakes(self.repo, self.plan))

    def test_supervision_repairs_index_match_when_durable_queue_record_is_missing(self) -> None:
        action = {"task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED", "reason": "done"}
        fingerprint = "001-work.md:run-a:REVIEW_REQUIRED"
        (self.repo / ".agent").mkdir()
        index = KANBAN.supervision_index_path(self.repo, self.plan)
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"001-work.md": fingerprint}), encoding="utf-8")

        with patch.object(KANBAN, "reconcile_actions", return_value=[action]):
            wakes = KANBAN.supervise_once(self.repo, self.plan)

        self.assertEqual(1, len(wakes))
        self.assertEqual(wakes, KANBAN.queued_wakes(self.repo, self.plan))

    def test_stop_guard_blocks_once_for_actionable_work(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        with patch("sys.stdin", io.StringIO("{}")), patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(2, KANBAN.command_stop_guard(self.repo, self.plan))
        self.assertIn("TURN WOULD END BLIND", error.getvalue())

    def test_stop_hook_install_and_uninstall_preserve_existing_hooks(self) -> None:
        hooks = self.repo / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}}), encoding="utf-8")

        KANBAN.command_install_stop_hook(self.repo, self.plan)
        KANBAN.command_uninstall_stop_hook(self.repo, self.plan)

        restored = json.loads(hooks.read_text(encoding="utf-8"))
        self.assertEqual("keep", restored["hooks"]["Stop"][0]["hooks"][0]["command"])

    def test_reconcile_requires_delivery_only_after_approved_review(self) -> None:
        write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
        self.write_runtime("run-a", "001-work.md", finished_at=datetime.now(UTC).isoformat(), exit_code=0)
        run = self.repo / ".agent" / self.plan / "runs" / "run-a"
        (run / "reports").mkdir(parents=True, exist_ok=True)
        (run / "reports" / "001-work.md").write_text("DONE\n", encoding="utf-8")
        (run / "reviews").mkdir(parents=True, exist_ok=True)
        (run / "reviews" / "001-work.md").write_text("Review status: approved\n", encoding="utf-8")

        self.assertEqual("DELIVERY_REQUIRED", KANBAN.reconcile_actions(self.repo, self.plan)[0]["action"])


if __name__ == "__main__":
    unittest.main()
