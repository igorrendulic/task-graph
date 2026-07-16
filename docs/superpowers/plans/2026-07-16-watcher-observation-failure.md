# Watcher Observation Failure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make watcher observation failures explicit instead of silently reporting a quiet system.

**Architecture:** Guard status collection in the read-only watcher. Checkpoints fail closed with exit code `2`; dashboard loops render the error and continue their normal retry cadence. No controller or worker path changes.

**Tech Stack:** Python standard library, `unittest`, `unittest.mock`.

## Global Constraints

- Keep `scripts/watcher.py` read-only: no wakes, controller mutations, or worker restarts.
- Preserve successful-observation no-worker, actionable-worker, and timeout checkpoint semantics.
- Use `observation error:` as concise caller-facing checkpoint output.

---

### Task 1: Handle and Retry Observation Failures

**Files:**
- Modify: `tests/test_watcher.py`
- Modify: `scripts/watcher.py`

**Interfaces:**
- Consumes: `KANBAN.collect_status(repo, plan, run_id, task_name) -> list[dict[str, object]]`.
- Produces: `watch_exec(..., checkpoint=True) -> 2` on a collection exception; dashboard loops render an observation error then retry.

- [ ] **Step 1: Write failing tests**

Add a checkpoint test that makes `collect_status` raise `OSError("runtime unavailable")`, asserts return code `2`, contains `observation error: runtime unavailable`, and verifies `time.sleep` was not called. Add a dashboard test whose first collection raises that error and whose next collection succeeds; assert the dashboard error and normal monitor rendering are both present.

- [ ] **Step 2: Verify red**

Run: `python3 -m unittest tests.test_watcher.WatcherTest.test_checkpoint_observation_error_returns_two_without_sleeping tests.test_watcher.WatcherTest.test_dashboard_retries_after_observation_error -v`

Expected: FAIL because collection exceptions currently escape `watch_exec`.

- [ ] **Step 3: Implement minimally**

Catch `OSError`, `ValueError`, and `json.JSONDecodeError` around each `collect_status` call. Checkpoints print the concise lower-case error and return `2`; dashboard paths render the capitalized error, sleep their normal interval, and continue.

- [ ] **Step 4: Verify focused suite**

Run: `python3 -m unittest tests.test_watcher -v`

Expected: PASS, including the existing checkpoint tests.

- [ ] **Step 5: Verify full suite and commit**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS with no failures.

Commit: `fix: surface watcher observation failures`.
