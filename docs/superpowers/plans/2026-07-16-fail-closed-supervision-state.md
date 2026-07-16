# Fail-Closed Supervision State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task.

**Goal:** Prevent corrupt durable supervision state from replaying or losing wake work.

**Architecture:** Add typed strict supervision readers in `scripts/kanban.py`, then make `scripts/controller.py` preflight all durable queue coordination artifacts before any wake transition.

**Tech Stack:** Python 3 standard library and `unittest`.

## Global Constraints

- Missing artifacts remain empty first-run state.
- Malformed JSON and non-object roots raise a typed error with the path.
- Queue errors include a one-based JSONL line.
- Corruption may not resume, claim, acknowledge, dispatch, or enqueue wakes.
- Recovery requires repair or replacement, then explicit controller start.

### Task 1: Strict readers and queue validation

**Files:** `scripts/kanban.py`, `tests/test_kanban.py`.

- [ ] Add failing tests for malformed claims, non-object dedupe state, missing artifacts, and missing `id`, `task`, `run_id`, or `action` in a queue record.
- [ ] Run `python3 -m unittest tests.test_kanban -v`; each new test must fail because the reader is still permissive.
- [ ] Add `SupervisionStateCorruption(path, detail, line=None)` and `load_supervision_json_object(path)`. Return `{}` only when missing; raise for decode errors and non-dict roots.
- [ ] Make queue records require non-empty string `id`, `task`, `run_id`, and `action`; report path and line through the typed error.
- [ ] Replace claims and dedupe loader call sites with the strict reader, then rerun `python3 -m unittest tests.test_kanban -v`.

### Task 2: Fail-closed controller preflight

**Files:** `scripts/controller.py`, `tests/test_controller.py`.

- [ ] Add a failing test: corrupt claims pause the controller, preserve an acknowledged wake, and make no resume or claim call.
- [ ] Add a failing test: a malformed second queue line pauses the controller and persists the queue path and line number.
- [ ] Run `python3 -m unittest tests.test_controller -v`; verify those tests fail before implementation.
- [ ] At the top of `drain_once`, read strict claims, strict dedupe state, and queue records before calling `resume_claimed_wakes`, `supervise_once`, `claim_wake`, acknowledgement, or dispatch.
- [ ] On `SupervisionStateCorruption`, persist lifecycle `paused` and a `SUPERVISION_STATE_CORRUPTION` pending alert with artifact, optional line, detail, and timestamp; return an empty result.
- [ ] Add a restart-after-repair test that preserves a `claimed` dispatch, repairs claims to `claimed`, and proves explicit restart resumes it without a fresh claim.
- [ ] Run `python3 -m unittest tests.test_controller -v` and commit the task.

### Task 3: Operator recovery documentation

**Files:** `README.md`, `SKILL.md`, `tests/test_skill_docs.py`.

- [ ] Add a failing docs assertion for `SUPERVISION_STATE_CORRUPTION`, “repair or replace”, and explicit controller start.
- [ ] Run `python3 -m unittest tests.test_skill_docs -v` and observe failure.
- [ ] Document that malformed claims, queue, or dedupe state pauses before work is changed; the operator repairs or replaces the named artifact and explicitly runs `controller.py start`. The controller neither auto-repairs nor auto-restarts.
- [ ] Run `python3 -m unittest discover -s tests -v` and commit the task.
