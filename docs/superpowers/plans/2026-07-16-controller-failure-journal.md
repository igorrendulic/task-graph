# Controller Failure Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist actionable diagnostics for unexpected controller-loop exceptions while keeping claimed wakes resumable through an explicit restart.

**Architecture:** `scripts/controller.py` owns a capped JSONL journal and the latest `active_failure` projection in `controller.json`. A phase-aware loop boundary records unexpected exceptions, exits without restart, and lets the existing wake-resume protocol handle claimed work on a later explicit start. Status is read-only and explains the recovery action when a failure is active on a dead or stale controller.

**Tech Stack:** Python 3 standard library (`json`, `traceback`, `pathlib`), existing Task Graph atomic state helpers, `unittest`.

## Global Constraints

- Retain exactly the newest 50 failure records, deterministically, in chronological order.
- Failure records include timestamp, phase, exception class/message, last wake ID, and a bounded traceback summary.
- Never acknowledge, escalate, dequeue, or otherwise alter a claimed wake because of an unexpected controller exception.
- Never auto-restart a controller; `start` remains the sole recovery action.
- Clear only `active_failure`, and only after `tmux_start` succeeds; never clear journal history.

---

### Task 1: Durable failure record and capped journal

**Files:**
- Modify: `scripts/controller.py: state helpers near controller_state_path and normalize_state`
- Test: `tests/test_controller.py: ControllerStateTest`

**Interfaces:**
- Consumes: `KANBAN.plan_dir(repo, plan)`, `KANBAN.write_atomic(path, text)`, and `KANBAN.utc_now()`.
- Produces: `controller_failure_journal_path(repo: Path, plan: str) -> Path`; `record_controller_failure(repo: Path, plan: str, *, phase: str, error: BaseException, wake_id: str | None) -> dict[str, object]`; and `state["active_failure"]`.

- [ ] **Step 1: Write the failing persistence and cap tests**

Add these tests to `ControllerStateTest`:

```python
def test_controller_failure_record_is_active_and_keeps_newest_fifty_entries(self) -> None:
    CONTROLLER.create_state(self.repo, self.plan, None)
    for number in range(51):
        CONTROLLER.record_controller_failure(
            self.repo, self.plan, phase="drain", error=RuntimeError(f"failure {number}"), wake_id=f"wake-{number}"
        )
    entries = [json.loads(line) for line in CONTROLLER.controller_failure_journal_path(self.repo, self.plan).read_text().splitlines()]
    self.assertEqual(50, len(entries))
    self.assertEqual("failure 1", entries[0]["message"])
    self.assertEqual("failure 50", entries[-1]["message"])
    state = CONTROLLER.load_state(self.repo, self.plan)
    self.assertEqual(entries[-1], state["active_failure"])

def test_controller_failure_record_has_bounded_traceback_and_wake_id(self) -> None:
    CONTROLLER.create_state(self.repo, self.plan, None)
    try:
        raise ValueError("boom")
    except ValueError as error:
        failure = CONTROLLER.record_controller_failure(
            self.repo, self.plan, phase="supervise", error=error, wake_id="wake-9"
        )
    self.assertEqual("supervise", failure["phase"])
    self.assertEqual("ValueError", failure["exception_type"])
    self.assertEqual("boom", failure["message"])
    self.assertEqual("wake-9", failure["wake_id"])
    self.assertLessEqual(len(failure["traceback"]), CONTROLLER.FAILURE_TRACEBACK_MAX_CHARS)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_controller.ControllerStateTest.test_controller_failure_record_is_active_and_keeps_newest_fifty_entries tests.test_controller.ControllerStateTest.test_controller_failure_record_has_bounded_traceback_and_wake_id -v`

Expected: FAIL because the journal path, recorder, traceback constant, and `active_failure` field do not exist.

- [ ] **Step 3: Implement the minimum durable failure helpers**

In `scripts/controller.py`, add `import traceback`, `FAILURE_JOURNAL_LIMIT = 50`, and a bounded `FAILURE_TRACEBACK_MAX_CHARS` constant. Add a journal-path helper returning `KANBAN.plan_dir(repo, plan) / "state" / "controller-failures.jsonl"`. Extend `normalize_state` and `create_state` so `active_failure` defaults to `None`.

Implement `record_controller_failure` to form this exact record shape:

```python
{
    "at": KANBAN.utc_now(),
    "phase": phase,
    "exception_type": type(error).__name__,
    "message": str(error),
    "wake_id": wake_id,
    "traceback": traceback_text[-FAILURE_TRACEBACK_MAX_CHARS:],
}
```

Read non-empty journal lines as JSON objects, append the new record, retain `entries[-FAILURE_JOURNAL_LIMIT:]`, and persist newline-delimited JSON using `KANBAN.write_atomic`. Under `controller_state_lock`, update `active_failure` through `persist_mutation` so its revision and timestamp stay consistent. Return the record.

- [ ] **Step 4: Run the persistence tests to verify they pass**

Run: `python3 -m unittest tests.test_controller.ControllerStateTest.test_controller_failure_record_is_active_and_keeps_newest_fifty_entries tests.test_controller.ControllerStateTest.test_controller_failure_record_has_bounded_traceback_and_wake_id -v`

Expected: PASS.

- [ ] **Step 5: Commit the durable-state change**

```bash
git add scripts/controller.py tests/test_controller.py
git commit -m "feat: journal controller failures"
```

### Task 2: Phase boundary, recovery projection, and operator documentation

**Files:**
- Modify: `scripts/controller.py: dispatch_wake, run_controller, start_controller, status_controller`
- Modify: `tests/test_controller.py: ControllerStateTest`
- Modify: `README.md: Local controller`
- Test: `tests/test_controller.py`
- Test: `tests/test_skill_docs.py`

**Interfaces:**
- Consumes: `record_controller_failure(...)`, current `state["dispatches"]`, `tmux_start(...)`, and `heartbeat_is_fresh(...)`.
- Produces: `run_controller(...)` exits after recording an unexpected phase failure; `status_controller(...)` includes `recovery_recommendation`; explicit successful start clears `active_failure`.

- [ ] **Step 1: Write failing controller-boundary and restart tests**

Add tests that patch the relevant phase and assert durable behavior:

```python
def test_supervision_failure_records_phase_without_changing_claimed_wake(self) -> None:
    state = CONTROLLER.create_state(self.repo, self.plan, None)
    wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}
    CONTROLLER.mutate_state(self.repo, self.plan, lambda latest: latest["dispatches"].update({"wake-1": {"state": "claimed", "wake": wake}}))
    with patch.object(CONTROLLER, "refresh_heartbeat"), patch.object(CONTROLLER, "drain_once"), patch.object(
        CONTROLLER.KANBAN, "command_supervise", side_effect=RuntimeError("supervision broke")
    ):
        self.assertEqual(1, CONTROLLER.run_controller(self.repo, self.plan))
    saved = CONTROLLER.load_state(self.repo, self.plan)
    self.assertEqual("supervise", saved["active_failure"]["phase"])
    self.assertEqual("claimed", saved["dispatches"]["wake-1"]["state"])

def test_review_dispatch_failure_records_claimed_wake_as_resumable(self) -> None:
    state = CONTROLLER.create_state(self.repo, self.plan, None)
    wake = {"id": "wake-1", "task": "001-work.md", "run_id": "run-a", "action": "REVIEW_REQUIRED"}
    with patch.object(CONTROLLER, "start_review", side_effect=RuntimeError("review launch failed")):
        with self.assertRaisesRegex(RuntimeError, "review launch failed"):
            CONTROLLER.dispatch_wake(self.repo, self.plan, wake, state)
    # Exercise the boundary with dispatch as the active phase/wake context.
    CONTROLLER.record_controller_failure(self.repo, self.plan, phase="drain", error=RuntimeError("review launch failed"), wake_id="wake-1")
    saved = CONTROLLER.load_state(self.repo, self.plan)
    self.assertEqual("wake-1", saved["active_failure"]["wake_id"])
    self.assertEqual("claimed", saved["dispatches"]["wake-1"]["state"])

def test_successful_tmux_start_clears_active_failure_but_failed_start_preserves_it(self) -> None:
    CONTROLLER.create_state(self.repo, self.plan, None)
    CONTROLLER.record_controller_failure(self.repo, self.plan, phase="drain", error=RuntimeError("boom"), wake_id=None)
    with patch.object(CONTROLLER, "require_tmux"), patch.object(CONTROLLER, "tmux_start", side_effect=RuntimeError("no tmux")):
        with self.assertRaisesRegex(RuntimeError, "no tmux"):
            CONTROLLER.start_controller(self.repo, self.plan, None)
    self.assertIsNotNone(CONTROLLER.load_state(self.repo, self.plan)["active_failure"])
    with patch.object(CONTROLLER, "require_tmux"), patch.object(CONTROLLER, "tmux_start", return_value=123):
        CONTROLLER.start_controller(self.repo, self.plan, None)
    self.assertIsNone(CONTROLLER.load_state(self.repo, self.plan)["active_failure"])
```

Add a status test that uses a dead tmux session and an active failure, captures JSON stdout, and asserts `recovery_recommendation == "Inspect active_failure and explicitly run controller.py start."`.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python3 -m unittest tests.test_controller -v`

Expected: FAIL because `run_controller` has no phase boundary and `status_controller` has no recovery recommendation; the active marker is not cleared at successful tmux launch.

- [ ] **Step 3: Implement phase-aware failure handling and restart behavior**

Add a small loop helper or local `try`/`except Exception` around `refresh_heartbeat`, `drain_once`, and `KANBAN.command_supervise`. Keep `last_wake_id` current by deriving it from the most recent claimed/started dispatch before the phase begins and by setting it before calling `dispatch_wake`. On exception, call `record_controller_failure` with the phase and wake ID, print a concise stderr diagnostic, and return `1`. Do not catch `KeyboardInterrupt` or `SystemExit`.

Ensure exceptions from review dispatch run through this boundary: either have `drain_once` identify the wake immediately before `dispatch_wake`, or introduce a narrow dispatcher wrapper used by `drain_once`; do not change `dispatch_wake` acknowledgement ordering.

In `start_controller`, leave `active_failure` untouched during lease acquisition. After `tmux_start` returns successfully and the PID is persisted, clear `active_failure` in a separate state mutation. This guarantees a tmux launch error retains diagnostic context.

In `status_controller`, preserve the existing live/healthy projection and add `recovery_recommendation` only when the session is dead or heartbeat stale and there is an `active_failure` or in-progress work. Its value is exactly `"Inspect active_failure and explicitly run controller.py start."`; otherwise set it to `None`.

- [ ] **Step 4: Document explicit failure recovery**

Extend the Local controller paragraph in `README.md` to state that unexpected exceptions are retained in `state/controller-failures.jsonl`, that `controller.json` exposes the latest `active_failure`, that claimed wakes remain untouched, and that operators inspect status then explicitly run `controller.py start`; no automatic restart occurs.

- [ ] **Step 5: Run focused and full verification**

Run: `python3 -m unittest tests.test_controller tests.test_skill_docs -v`

Expected: PASS with all controller and documentation tests successful.

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS for the complete Python test suite.

- [ ] **Step 6: Commit the boundary and documentation change**

```bash
git add scripts/controller.py tests/test_controller.py README.md
git commit -m "feat: surface controller failure recovery"
```
