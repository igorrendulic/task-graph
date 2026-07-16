import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "scripts" / "controller.py"
SPEC = importlib.util.spec_from_file_location("controller", CONTROLLER_PATH)
assert SPEC and SPEC.loader
CONTROLLER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTROLLER
SPEC.loader.exec_module(CONTROLLER)


class ControllerStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.plan = "sample-plan"
        (self.repo / ".agent" / self.plan).mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_state_persists_plan_configuration(self) -> None:
        state = CONTROLLER.create_state(self.repo, self.plan, "make check")

        saved = json.loads(CONTROLLER.controller_state_path(self.repo, self.plan).read_text())
        self.assertEqual("running", state["lifecycle"])
        self.assertEqual("make check", saved["no_mistakes_command"])
        self.assertEqual(CONTROLLER.controller_session_name(self.plan), saved["session"])

    def test_status_does_not_overwrite_an_acknowledged_dispatch(self) -> None:
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}

        def acknowledge_during_status(_session: str) -> bool:
            CONTROLLER.mutate_state(
                self.repo,
                self.plan,
                lambda current: current["dispatches"].update({"wake-1": {"state": "acknowledged", "wake": wake}}),
            )
            return False

        with patch.object(CONTROLLER.KANBAN, "tmux_session_exists", side_effect=acknowledge_during_status), patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            CONTROLLER.status_controller(self.repo, self.plan)

        saved = CONTROLLER.load_state(self.repo, self.plan)
        self.assertEqual("acknowledged", saved["dispatches"]["wake-1"]["state"])
        self.assertEqual(state["revision"] + 1, saved["revision"])

    def test_stop_wins_over_a_heartbeat_update(self) -> None:
        stale_state = CONTROLLER.create_state(self.repo, self.plan, None).copy()
        heartbeat_started = threading.Event()
        heartbeat_finished = threading.Event()
        original_write_state = CONTROLLER.write_state

        def start_heartbeat_after_stopped_state_is_written(repo: Path, plan: str, state: dict[str, object]) -> None:
            original_write_state(repo, plan, state)
            if state.get("lifecycle") != "stopped":
                return
            thread = threading.Thread(
                target=lambda: (heartbeat_started.set(), CONTROLLER.refresh_heartbeat(self.repo, self.plan, stale_state), heartbeat_finished.set())
            )
            thread.start()
            self.assertTrue(heartbeat_started.wait(timeout=1))

        with patch.object(CONTROLLER.subprocess, "run"), patch.object(
            CONTROLLER.KANBAN, "tmux_session_exists", return_value=False
        ), patch.object(
            CONTROLLER, "write_state", side_effect=start_heartbeat_after_stopped_state_is_written
        ):
            CONTROLLER.stop_controller(self.repo, self.plan)

        self.assertTrue(heartbeat_finished.wait(timeout=1))
        saved = CONTROLLER.load_state(self.repo, self.plan)
        self.assertEqual("stopped", saved["lifecycle"])
        self.assertIsNone(saved["lease"])

    def test_state_revision_advances_once_per_successful_mutation(self) -> None:
        created = CONTROLLER.create_state(self.repo, self.plan, None)
        created_revision = created["revision"]
        CONTROLLER.refresh_heartbeat(self.repo, self.plan, created)
        after_heartbeat = CONTROLLER.load_state(self.repo, self.plan)
        heartbeat_revision = after_heartbeat["revision"]
        with patch.object(CONTROLLER.KANBAN, "escalate_wake"):
            CONTROLLER.pause_for_alert(
                self.repo,
                self.plan,
                after_heartbeat,
                {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "USER_CONTEXT_REQUIRED"},
                "USER_CONTEXT_REQUIRED",
            )
        after_pause = CONTROLLER.load_state(self.repo, self.plan)

        self.assertEqual(1, created_revision)
        self.assertEqual(2, heartbeat_revision)
        self.assertEqual(3, after_pause["revision"])

    def test_claimed_wake_is_acknowledged_only_after_dispatch_starts(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}
        CONTROLLER.create_state(self.repo, self.plan, None)
        with patch.object(CONTROLLER.KANBAN, "claim_wake", return_value=wake) as claim, patch.object(
            CONTROLLER, "start_review", return_value="review-session"
        ) as start, patch.object(CONTROLLER.KANBAN, "acknowledge_wake") as acknowledge:
            CONTROLLER.dispatch_wake(self.repo, self.plan, wake, {"lifecycle": "running", "dispatches": {}})

        claim.assert_called_once_with(self.repo, self.plan, "wake-1")
        start.assert_called_once()
        acknowledge.assert_called_once_with(self.repo, self.plan, "wake-1")

    def test_failed_dispatch_leaves_claimed_wake_unacknowledged(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}
        CONTROLLER.create_state(self.repo, self.plan, None)
        with patch.object(CONTROLLER.KANBAN, "claim_wake", return_value=wake), patch.object(
            CONTROLLER, "start_review", side_effect=RuntimeError("tmux unavailable")
        ), patch.object(CONTROLLER.KANBAN, "acknowledge_wake") as acknowledge:
            with self.assertRaisesRegex(RuntimeError, "tmux unavailable"):
                CONTROLLER.dispatch_wake(self.repo, self.plan, wake, {"lifecycle": "running", "dispatches": {}})

        acknowledge.assert_not_called()

    def test_human_required_wake_pauses_and_escalates_without_acknowledging(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "USER_CONTEXT_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        with patch.object(CONTROLLER.KANBAN, "claim_wake", return_value=wake), patch.object(
            CONTROLLER.KANBAN, "escalate_wake"
        ) as escalate, patch.object(CONTROLLER.KANBAN, "acknowledge_wake") as acknowledge:
            result = CONTROLLER.dispatch_wake(self.repo, self.plan, wake, state)

        self.assertEqual("USER_CONTEXT_REQUIRED", result)
        self.assertEqual("paused", state["lifecycle"])
        self.assertEqual("USER_CONTEXT_REQUIRED", state["pending_alert"]["reason"])
        escalate.assert_called_once_with(self.repo, self.plan, "wake-1")
        acknowledge.assert_not_called()

    def test_alert_is_persisted_before_a_failed_escalation_transition(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "USER_CONTEXT_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        with patch.object(CONTROLLER.KANBAN, "claim_wake", return_value=wake), patch.object(
            CONTROLLER.KANBAN, "escalate_wake", side_effect=RuntimeError("disk failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "disk failure"):
                CONTROLLER.dispatch_wake(self.repo, self.plan, wake, state)

        saved = CONTROLLER.load_state(self.repo, self.plan)
        self.assertEqual("paused", saved["lifecycle"])
        self.assertEqual("USER_CONTEXT_REQUIRED", saved["pending_alert"]["reason"])

    def test_delivery_checkpoint_pauses_and_escalates(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        with patch.object(CONTROLLER.KANBAN, "claim_wake", return_value=wake), patch.object(
            CONTROLLER, "deliver", return_value="DELIVERY_APPROVAL_REQUIRED"
        ), patch.object(CONTROLLER.KANBAN, "escalate_wake") as escalate:
            CONTROLLER.dispatch_wake(self.repo, self.plan, wake, state)

        self.assertEqual("paused", state["lifecycle"])
        self.assertEqual("DELIVERY_APPROVAL_REQUIRED", state["pending_alert"]["reason"])
        escalate.assert_called_once_with(self.repo, self.plan, "wake-1")

    def test_pause_leaves_later_queued_wakes_for_a_future_start(self) -> None:
        human = {"id": "human", "task": "001-work.md", "run_id": "run-a", "action": "USER_CONTEXT_REQUIRED"}
        autonomous = {"id": "auto", "task": "002-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        with patch.object(CONTROLLER, "resume_claimed_wakes"), patch.object(
            CONTROLLER, "reconcile_reviewer_dispatches"
        ), patch.object(CONTROLLER.KANBAN, "supervise_once"), patch.object(
            CONTROLLER.KANBAN, "queued_wakes", return_value=[human, autonomous]
        ), patch.object(CONTROLLER.KANBAN, "load_json_object", return_value={}), patch.object(
            CONTROLLER.KANBAN, "claim_wake", return_value=human
        ), patch.object(CONTROLLER.KANBAN, "escalate_wake"), patch.object(CONTROLLER, "start_review") as review:
            CONTROLLER.drain_once(self.repo, self.plan, state)

        review.assert_not_called()

    def test_restart_resumes_claimed_wake_without_claiming_again(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        state["dispatches"] = {"wake-1": {"state": "claimed", "wake": wake}}
        CONTROLLER.write_state(self.repo, self.plan, state)
        with patch.object(CONTROLLER, "start_review", return_value="review-session"), patch.object(
            CONTROLLER.KANBAN, "claim_wake"
        ) as claim, patch.object(CONTROLLER.KANBAN, "acknowledge_wake") as acknowledge:
            CONTROLLER.resume_claimed_wakes(self.repo, self.plan, state)

        claim.assert_not_called()
        acknowledge.assert_called_once_with(self.repo, self.plan, "wake-1")

    def test_restart_acknowledges_started_wake_without_replaying_dispatch(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        state["dispatches"] = {"wake-1": {"state": "started", "wake": wake, "result": "review-session"}}
        CONTROLLER.write_state(self.repo, self.plan, state)

        with patch.object(CONTROLLER, "start_review") as review, patch.object(
            CONTROLLER, "start_repair"
        ) as repair, patch.object(CONTROLLER, "deliver") as deliver, patch.object(
            CONTROLLER.KANBAN, "acknowledge_wake"
        ) as acknowledge:
            CONTROLLER.resume_claimed_wakes(self.repo, self.plan, state)

        review.assert_not_called()
        repair.assert_not_called()
        deliver.assert_not_called()
        acknowledge.assert_called_once_with(self.repo, self.plan, "wake-1")
        self.assertEqual("acknowledged", state["dispatches"]["wake-1"]["state"])

    def test_failed_started_wake_acknowledgement_remains_durable_for_retry(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REPAIR_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        state["dispatches"] = {"wake-1": {"state": "started", "wake": wake, "result": "repair-session"}}
        CONTROLLER.write_state(self.repo, self.plan, state)

        with patch.object(CONTROLLER, "start_repair") as repair, patch.object(
            CONTROLLER.KANBAN, "acknowledge_wake", side_effect=OSError("disk failure")
        ) as acknowledge:
            with self.assertRaisesRegex(OSError, "disk failure"):
                CONTROLLER.resume_claimed_wakes(self.repo, self.plan, state)

        repair.assert_not_called()
        acknowledge.assert_called_once_with(self.repo, self.plan, "wake-1")
        saved = CONTROLLER.load_state(self.repo, self.plan)
        self.assertEqual("started", saved["dispatches"]["wake-1"]["state"])

    def test_delivery_without_yolo_records_an_approval_checkpoint(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="AWAIT_LOCAL_APPROVAL"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), {"worktree": "/work"})
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "local-only", "yolo": False}), patch.object(
            CONTROLLER.subprocess, "run"
        ) as run:
            result = CONTROLLER.deliver(self.repo, self.plan, wake, {"no_mistakes_command": None})

        self.assertEqual("DELIVERY_APPROVAL_REQUIRED", result)
        run.assert_not_called()

    def test_no_mistakes_requires_the_controller_gate_command(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="RUN_NO_MISTAKES"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), {"worktree": "/work"})
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "no-mistakes", "yolo": True}):
            result = CONTROLLER.deliver(self.repo, self.plan, wake, {"no_mistakes_command": None})

        self.assertEqual("NO_MISTAKES_COMMAND_REQUIRED", result)

    def test_no_mistakes_gate_failure_retains_work(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        completed = __import__("subprocess").CompletedProcess
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="RUN_NO_MISTAKES"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), {"worktree": "/work"})
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "no-mistakes", "yolo": True}), patch.object(
            CONTROLLER, "run_external", return_value=CONTROLLER.ExternalRun(completed([], 1))
        ), patch.object(CONTROLLER, "finalize_landed") as finalize:
            result = CONTROLLER.deliver(self.repo, self.plan, wake, {"no_mistakes_command": "make check"})

        self.assertEqual("NO_MISTAKES_FAILED", result)
        finalize.assert_not_called()

    def test_hung_no_mistakes_times_out_and_refreshes_heartbeat(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, "make check")
        record = {"worktree": "/work", "branch": "task-branch"}
        completed = __import__("subprocess").CompletedProcess
        heartbeats: list[None] = []
        def timeout_with_heartbeat(*_args: object, **kwargs: object) -> object:
            kwargs["heartbeat"]()
            return CONTROLLER.ExternalRun(None, timed_out=True)

        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="RUN_NO_MISTAKES"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), record)
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "no-mistakes", "yolo": True}), patch.object(
            CONTROLLER, "run_external", side_effect=timeout_with_heartbeat
        ), patch.object(CONTROLLER, "refresh_heartbeat", side_effect=lambda *_: heartbeats.append(None)), patch.object(
            CONTROLLER, "finalize_landed"
        ) as finalize:
            result = CONTROLLER.deliver(self.repo, self.plan, wake, state)

        self.assertEqual("NO_MISTAKES_TIMEOUT", result)
        self.assertTrue(heartbeats)
        finalize.assert_not_called()

    def test_polling_runner_terminates_a_hung_command_after_refreshing_heartbeat(self) -> None:
        class HungProcess:
            returncode = None

            def __init__(self) -> None:
                self.terminated = False

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                return "", ""

        process = HungProcess()
        heartbeats: list[None] = []
        with patch.object(CONTROLLER.subprocess, "Popen", return_value=process), patch.object(
            CONTROLLER.time, "monotonic", side_effect=[0, 0, 2]
        ), patch.object(CONTROLLER.time, "sleep"):
            result = CONTROLLER.run_external(
                ["hung-command"], cwd=self.repo, timeout_seconds=1, heartbeat=lambda: heartbeats.append(None)
            )

        self.assertTrue(result.timed_out)
        self.assertTrue(process.terminated)
        self.assertGreaterEqual(len(heartbeats), 2)

    def test_pr_check_timeout_pauses_without_recording_delivery(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        record = {"worktree": "/work", "branch": "task-branch", "head_commit": "expected-head"}
        completed = __import__("subprocess").CompletedProcess
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="MERGE_GREEN_PR"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), record)
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}), patch.object(
            CONTROLLER, "run_external", side_effect=[
                CONTROLLER.ExternalRun(completed([], 0)), CONTROLLER.ExternalRun(completed([], 0)),
                CONTROLLER.ExternalRun(None, timed_out=True),
            ]
        ), patch.object(CONTROLLER.KANBAN, "escalate_wake"), patch.object(
            CONTROLLER.KANBAN, "command_record_delivery"
        ) as record_delivery:
            result = CONTROLLER.dispatch_wake(self.repo, self.plan, wake, state, claimed=True)

        self.assertEqual("PR_CHECKS_TIMEOUT", result)
        self.assertEqual("paused", state["lifecycle"])
        record_delivery.assert_not_called()

    def test_merge_timeout_records_unknown_outcome_and_never_retries_merge(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        record = {"worktree": "/work", "branch": "task-branch", "head_commit": "expected-head"}
        completed = __import__("subprocess").CompletedProcess
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="MERGE_GREEN_PR"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), record)
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}), patch.object(
            CONTROLLER, "run_external", side_effect=[
                CONTROLLER.ExternalRun(completed([], 0)), CONTROLLER.ExternalRun(completed([], 0)),
                CONTROLLER.ExternalRun(completed([], 0)), CONTROLLER.ExternalRun(None, timed_out=True),
            ]
        ) as runner, patch.object(CONTROLLER.KANBAN, "escalate_wake"):
            self.assertEqual("DELIVERY_OUTCOME_UNKNOWN", CONTROLLER.dispatch_wake(self.repo, self.plan, wake, state, claimed=True))
            self.assertEqual("DELIVERY_OUTCOME_UNKNOWN", CONTROLLER.dispatch_wake(self.repo, self.plan, wake, state, claimed=True))

        merge_commands = [call.args[0] for call in runner.call_args_list if call.args[0][:3] == ["gh", "pr", "merge"]]
        self.assertEqual(1, len(merge_commands))
        self.assertEqual("expected-head", state["delivery_attempt"]["expected_head"])

    def test_resume_finalizes_a_confirmed_expected_merge_once(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        state.update({
            "lifecycle": "paused",
            "pending_alert": {"task": "001-work.md", "run_id": "run-a", "reason": "DELIVERY_OUTCOME_UNKNOWN"},
            "delivery_attempt": {"task": "001-work.md", "run_id": "run-a", "branch": "task-branch", "expected_head": "expected-head", "state": "submitted"},
        })
        CONTROLLER.write_state(self.repo, self.plan, state)
        confirmed = CONTROLLER.ExternalRun(__import__("subprocess").CompletedProcess([], 0, stdout=json.dumps({
            "state": "MERGED", "headRefOid": "expected-head"
        })))
        with patch.object(CONTROLLER, "require_tmux"), patch.object(
            CONTROLLER.KANBAN, "tmux_session_exists", return_value=False
        ), patch.object(CONTROLLER, "tmux_start", return_value=123), patch.object(
            CONTROLLER, "run_external", return_value=confirmed
        ), patch.object(CONTROLLER, "finalize_landed"
        ) as finalize:
            CONTROLLER.start_controller(self.repo, self.plan, None)
            CONTROLLER.start_controller(self.repo, self.plan, None)

        finalize.assert_called_once_with(self.repo, self.plan, "run-a", "001-work.md")

    def test_start_refuses_a_second_live_controller(self) -> None:
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        with patch.object(CONTROLLER, "require_tmux"), patch.object(
            CONTROLLER.KANBAN, "tmux_session_exists", return_value=True
        ), patch.object(CONTROLLER, "tmux_start") as start:
            with self.assertRaisesRegex(SystemExit, "live controller"):
                CONTROLLER.start_controller(self.repo, self.plan, None)

        self.assertEqual("running", state["lifecycle"])
        start.assert_not_called()

    def test_start_resumes_dead_controller_state_without_losing_dispatches(self) -> None:
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        state["dispatches"] = {"wake-1": {"state": "acknowledged"}}
        CONTROLLER.write_state(self.repo, self.plan, state)
        with patch.object(CONTROLLER, "require_tmux"), patch.object(
            CONTROLLER.KANBAN, "tmux_session_exists", return_value=False
        ), patch.object(CONTROLLER, "tmux_start", return_value=321):
            CONTROLLER.start_controller(self.repo, self.plan, "make check")

        restored = CONTROLLER.load_state(self.repo, self.plan)
        self.assertEqual(321, restored["pid"])
        self.assertEqual("make check", restored["no_mistakes_command"])
        self.assertIn("wake-1", restored["dispatches"])

    def test_status_projects_recovery_for_dead_running_controller_with_work_without_persisting_it(self) -> None:
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        (self.repo / ".agent" / self.plan / "in-progress").mkdir()
        (self.repo / ".agent" / self.plan / "in-progress" / "001-work.md").write_text("# Work\n")
        with patch.object(CONTROLLER.KANBAN, "tmux_session_exists", return_value=False), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            CONTROLLER.status_controller(self.repo, self.plan)

        saved = CONTROLLER.load_state(self.repo, self.plan)
        reported = json.loads(output.getvalue())
        self.assertFalse(saved["recovery_required"])
        self.assertIsNone(saved["recovery_alert"])
        self.assertTrue(reported["recovery_required"])
        self.assertEqual("CONTROLLER_RECOVERY_REQUIRED", reported["recovery_alert"]["reason"])

    def test_start_refuses_unresolved_persisted_alert(self) -> None:
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        state.update({"lifecycle": "paused", "pending_alert": {"task": "001-work.md", "run_id": "run-a", "reason": "DELIVERY_APPROVAL_REQUIRED"}})
        CONTROLLER.write_state(self.repo, self.plan, state)
        (self.repo / ".agent" / self.plan / "in-progress").mkdir()
        (self.repo / ".agent" / self.plan / "in-progress" / "001-work.md").write_text("# Work\n")
        with patch.object(CONTROLLER, "require_tmux"), patch.object(CONTROLLER, "tmux_start") as start:
            with self.assertRaisesRegex(SystemExit, "Pending controller alert"):
                CONTROLLER.start_controller(self.repo, self.plan, None)
        start.assert_not_called()

    def test_direct_pr_pushes_then_creates_checks_and_merges(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        record = {"worktree": "/work", "branch": "task-branch", "head_commit": "expected-head"}
        completed = __import__("subprocess").CompletedProcess
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="MERGE_GREEN_PR"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), record)
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}), patch.object(
            CONTROLLER, "run_external", return_value=CONTROLLER.ExternalRun(completed([], 0))
        ) as run, patch.object(CONTROLLER, "finalize_landed") as finalize:
            result = CONTROLLER.deliver(self.repo, self.plan, wake, state)

        self.assertEqual("LANDED", result)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["git", "push", "-u", "origin", "task-branch"], commands)
        self.assertIn(["gh", "pr", "checks", "task-branch", "--watch", "--fail-fast"], commands)
        finalize.assert_called_once_with(self.repo, self.plan, "run-a", "001-work.md")

    def test_missing_delivery_tool_retains_work_at_tooling_checkpoint(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        record = {"worktree": "/work", "branch": "task-branch", "head_commit": "expected-head"}
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="MERGE_GREEN_PR"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), record)
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}), patch.object(
            CONTROLLER, "run_external", return_value=CONTROLLER.ExternalRun(None, missing=True)
        ), patch.object(CONTROLLER, "finalize_landed") as finalize:
            result = CONTROLLER.deliver(self.repo, self.plan, wake, {})

        self.assertEqual("TOOLING_REQUIRED", result)
        finalize.assert_not_called()

    def test_failed_pr_checks_retain_work(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        record = {"worktree": "/work", "branch": "task-branch", "head_commit": "expected-head"}
        completed = __import__("subprocess").CompletedProcess
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="MERGE_GREEN_PR"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), record)
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}), patch.object(
            CONTROLLER, "run_external", side_effect=[
                CONTROLLER.ExternalRun(completed([], 0)), CONTROLLER.ExternalRun(completed([], 0)),
                CONTROLLER.ExternalRun(completed([], 1)),
            ]
        ), patch.object(CONTROLLER, "finalize_landed") as finalize:
            result = CONTROLLER.deliver(self.repo, self.plan, wake, {})

        self.assertEqual("PR_DELIVERY_FAILED", result)
        finalize.assert_not_called()

    def test_malformed_completed_review_becomes_a_verdict_checkpoint(self) -> None:
        run = self.repo / ".agent" / self.plan / "runs" / "run-a"
        (run / "reviews").mkdir(parents=True)
        (run / "reviews" / "001-work.md").write_text("not a verdict\n", encoding="utf-8")
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}
        state = CONTROLLER.create_state(self.repo, self.plan, None)
        state = CONTROLLER.mutate_state(
            self.repo,
            self.plan,
            lambda current: current["dispatches"].update(
                {"wake-1": {"state": "acknowledged", "wake": wake, "result": "review-session"}}
            ),
        )
        with patch.object(CONTROLLER.KANBAN, "tmux_liveness", return_value="IDLE_OR_DEAD"):
            CONTROLLER.reconcile_reviewer_dispatches(self.repo, self.plan, state)

        self.assertEqual("REVIEW_VERDICT_REQUIRED", state["dispatches"]["wake-1"]["result"])

    def test_local_delivery_refuses_dirty_integration_branch(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        record = {"worktree": "/work", "branch": "task-branch"}
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="FAST_FORWARD_LOCAL"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), record)
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "local-only", "yolo": True}), patch.object(
            CONTROLLER.KANBAN, "git_output", return_value=" M existing.txt\n"
        ), patch.object(CONTROLLER.subprocess, "run") as run:
            result = CONTROLLER.deliver(self.repo, self.plan, wake, {})

        self.assertEqual("LOCAL_DELIVERY_DIRTY", result)
        run.assert_not_called()

    def test_local_delivery_retains_work_when_fast_forward_fails(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "DELIVERY_REQUIRED"}
        record = {"worktree": "/work", "branch": "task-branch"}
        completed = __import__("subprocess").CompletedProcess
        with patch.object(CONTROLLER.KANBAN, "command_delivery_ready", return_value="FAST_FORWARD_LOCAL"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("", Path("/run"), record)
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "local-only", "yolo": True}), patch.object(
            CONTROLLER.KANBAN, "git_output", return_value=""
        ), patch.object(CONTROLLER.subprocess, "run", return_value=completed([], 1, stderr="not fast-forward")), patch.object(
            CONTROLLER, "finalize_landed"
        ) as finalize:
            result = CONTROLLER.deliver(self.repo, self.plan, wake, {})

        self.assertEqual("LOCAL_DELIVERY_NOT_FAST_FORWARD", result)
        finalize.assert_not_called()

    def test_repair_creates_one_inherited_child_attempt_from_parent_branch(self) -> None:
        parent = self.repo / ".agent" / self.plan / "runs" / "run-a"
        (parent / "reviews").mkdir(parents=True)
        (parent / "reviews" / "001-work.md").write_text(
            "Review status: changes_requested\n- add a regression test\n", encoding="utf-8"
        )
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REPAIR_REQUIRED"}
        runtime = ("run-a", parent, {"branch": "task-branch", "worktree": "/work"})
        completed = __import__("subprocess").CompletedProcess
        with patch.object(CONTROLLER, "require_tmux"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=runtime
        ), patch.object(CONTROLLER.KANBAN, "repair_attempt", return_value=None), patch.object(
            CONTROLLER.KANBAN, "reserve_repair_attempt", return_value={
                "attempt": 1,
                "child_run_id": "run-a-task001-repair1",
                "branch": "task-branch-repair-1",
                "worktree": "/tmp/repair-worktree",
                "phase": "reserved",
            }
        ) as reserve_attempt, patch.object(CONTROLLER.KANBAN, "mark_repair_attempt_phase") as mark_phase, patch.object(
            CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}
        ), patch.object(
            CONTROLLER.KANBAN, "create_child_worktree", return_value=Path("/tmp/repair-worktree")
        ) as create_worktree, patch.object(CONTROLLER.KANBAN, "command_launch_exec") as launch, patch.object(
            CONTROLLER.KANBAN, "runtime_record_for_run", return_value=("run-a-task001-repair1", parent, {"session": "repair-session"})
        ):
            CONTROLLER.start_repair(self.repo, self.plan, wake)

        reserve_attempt.assert_called_once()
        mark_phase.assert_called_once_with(self.repo, self.plan, "001-work.md", "launched")
        create_worktree.assert_called_once()
        self.assertEqual("task-branch", create_worktree.call_args.args[1])
        self.assertEqual("task-branch-repair-1", create_worktree.call_args.args[2])
        launch.assert_called_once()
        child = self.repo / ".agent" / self.plan / "runs" / "run-a-task001-repair1"
        self.assertEqual({"mode": "direct-pr", "yolo": True}, json.loads((child / "policy.json").read_text()))
        self.assertIn("add a regression test", (child / "briefs" / "001-work.md").read_text())

    def test_repair_worktree_failure_does_not_consume_attempt(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REPAIR_REQUIRED"}
        parent = self.repo / ".agent" / self.plan / "runs" / "run-a"
        runtime = ("run-a", parent, {"branch": "task-branch", "worktree": "/work"})
        with patch.object(CONTROLLER, "require_tmux"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=runtime
        ), patch.object(CONTROLLER.KANBAN, "repair_attempt", return_value=None), patch.object(
            CONTROLLER.KANBAN, "reserve_repair_attempt", return_value={
                "attempt": 1, "child_run_id": "run-a-task001-repair1", "branch": "task-branch-repair-1",
                "worktree": "/tmp/repair-worktree", "phase": "reserved",
            }
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}
        ), patch.object(CONTROLLER.KANBAN, "create_child_worktree", side_effect=SystemExit("failed")), patch.object(
            CONTROLLER.KANBAN, "mark_repair_attempt_phase"
        ) as mark_phase:
            self.assertEqual("REPAIR_REQUIRED", CONTROLLER.start_repair(self.repo, self.plan, wake))

        mark_phase.assert_called_once_with(self.repo, self.plan, "001-work.md", "failed")

    def test_repair_restart_reuses_reserved_identity(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REPAIR_REQUIRED"}
        parent = self.repo / ".agent" / self.plan / "runs" / "run-a"
        runtime = ("run-a", parent, {"branch": "task-branch", "worktree": "/work"})
        reservation = {
            "attempt": 1, "child_run_id": "run-a-task001-repair1", "branch": "task-branch-repair-1",
            "worktree": "/tmp/repair-worktree", "phase": "reserved",
        }
        with patch.object(CONTROLLER, "require_tmux"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=runtime
        ), patch.object(CONTROLLER.KANBAN, "runtime_record_for_run", return_value=None), patch.object(
            CONTROLLER.KANBAN, "repair_attempt", return_value=reservation), patch.object(
            CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}
        ), patch.object(CONTROLLER.KANBAN, "create_child_worktree", return_value=Path("/tmp/repair-worktree")) as create, patch.object(
            CONTROLLER.KANBAN, "command_launch_exec"
        ), patch.object(CONTROLLER.KANBAN, "mark_repair_attempt_phase"):
            CONTROLLER.start_repair(self.repo, self.plan, wake)

        create.assert_called_once_with(self.repo, "task-branch", "task-branch-repair-1", Path("/tmp/repair-worktree"))

    def test_repair_restart_uses_existing_runtime_without_duplicate_launch(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REPAIR_REQUIRED"}
        parent = self.repo / ".agent" / self.plan / "runs" / "run-a"
        parent_runtime = ("run-a", parent, {"branch": "task-branch", "worktree": "/work"})
        child_runtime = ("run-a-task001-repair1", parent, {"session": "repair-session"})
        reservation = {
            "attempt": 1, "child_run_id": "run-a-task001-repair1", "branch": "task-branch-repair-1",
            "worktree": "/tmp/repair-worktree", "phase": "reserved",
        }
        with patch.object(CONTROLLER, "require_tmux"), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=parent_runtime
        ), patch.object(CONTROLLER.KANBAN, "runtime_record_for_run", return_value=child_runtime), patch.object(
            CONTROLLER.KANBAN, "repair_attempt", return_value=reservation), patch.object(
            CONTROLLER.KANBAN, "mark_repair_attempt_phase"
        ) as mark_phase, patch.object(CONTROLLER.KANBAN, "command_launch_exec") as launch:
            self.assertEqual("repair-session", CONTROLLER.start_repair(self.repo, self.plan, wake))

        mark_phase.assert_called_once_with(self.repo, self.plan, "001-work.md", "launched")
        launch.assert_not_called()

    def test_repair_conflicting_reserved_worktree_requires_inspection(self) -> None:
        wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REPAIR_REQUIRED"}
        parent = self.repo / ".agent" / self.plan / "runs" / "run-a"
        worktree = self.repo / "reserved-worktree"
        worktree.mkdir()
        reservation = {
            "attempt": 1, "child_run_id": "run-a-task001-repair1", "branch": "task-branch-repair-1",
            "worktree": str(worktree), "phase": "reserved",
        }
        with patch.object(CONTROLLER, "require_tmux"), patch.object(
            CONTROLLER.KANBAN, "runtime_record_for_run", return_value=None
        ), patch.object(CONTROLLER.KANBAN, "repair_attempt", return_value=reservation), patch.object(
            CONTROLLER.KANBAN, "latest_runtime_record", return_value=("run-a", parent, {"branch": "task-branch"})
        ), patch.object(CONTROLLER.KANBAN, "read_run_policy", return_value={"mode": "direct-pr", "yolo": True}), patch.object(
            CONTROLLER.KANBAN, "verified_worktree", side_effect=SystemExit("conflict")
        ), patch.object(CONTROLLER.KANBAN, "command_launch_exec") as launch:
            self.assertEqual("INSPECTION_REQUIRED", CONTROLLER.start_repair(self.repo, self.plan, wake))

        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
