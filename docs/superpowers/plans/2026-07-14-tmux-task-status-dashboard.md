# tmux Task Status Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Launch a reserved task through tmux and observe all task executions without changing kanban state.

**Architecture:** `scripts/kanban.py` will own durable runtime JSON records and derive a shared status model from those records, tmux, and final reports. `launch-exec` will write the record before starting a wrapper in a deterministic tmux session; `status` will only read files and tmux state.

**Tech Stack:** Python standard library, tmux, Codex CLI, unittest.

## Global Constraints

- Unattended local execution requires tmux and uses `codex exec --sandbox workspace-write --ask-for-approval never`.
- Do not use an unsandboxed Codex option.
- `status` and `status --watch` never move task files, update ledgers, relaunch work, or clean up artifacts.

---

### Task 1: Runtime records and tmux launcher

**Files:**
- Modify: `scripts/kanban.py`
- Test: `tests/test_kanban.py`

- [ ] Write failing tests for deterministic session names, runtime JSON validation, and missing-tmux refusal.
- [ ] Run the focused tests and confirm they fail because the launcher/model is absent.
- [ ] Implement runtime-record helpers and `launch-exec`, writing its record before invoking tmux.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Read-only status model and live rendering

**Files:**
- Modify: `scripts/kanban.py`
- Test: `tests/test_kanban.py`

- [ ] Write failing tests for every status classification, filters, JSON schema, hints, and no-mutation watch rendering.
- [ ] Run the focused tests and confirm they fail because the status model is absent.
- [ ] Implement scan/filter/classify/render functions and wire `status --watch --interval` to the same model.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Documentation contract

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `tests/test_skill_docs.py`

- [ ] Write failing documentation-contract assertions for tmux launch and status examples.
- [ ] Run the documentation tests and confirm they fail.
- [ ] Document launch, one-shot, JSON, focused, attach, and watch workflows.
- [ ] Run the full suite and confirm it passes.
