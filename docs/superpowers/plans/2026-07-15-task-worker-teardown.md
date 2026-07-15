# Task Worker Teardown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean up each landed unattended task's worktree and preserved tmux session, and document exactly when that cleanup occurs.

**Architecture:** `command_teardown` remains the sole guarded cleanup entry point. After its existing landed/discard and clean-worktree checks, it will remove the dedicated Git worktree and then best-effort remove the recorded tmux session; an absent session is already-clean state. The skill and README will state that this occurs after a task commit is integrated and verified, before the task is marked done, rather than at final-PR time.

**Tech Stack:** Python 3 standard library, Git CLI, tmux, `unittest`, Markdown.

## Global Constraints

- Preserve the existing protection against removing dirty or unlanded work without explicit `--discard`.
- Preserve exited tmux panes until review, integration, and verification complete.
- Treat a missing tmux session as successful cleanup; fail teardown if `tmux kill-session` reports another error.
- Apply cleanup per task after `record-delivery --result landed`; do not wait for a final PR.

---

### Task 1: Guarded tmux cleanup and lifecycle documentation

**Files:**
- Modify: `scripts/kanban.py:723-742`
- Modify: `tests/test_kanban.py:500-515`
- Modify: `SKILL.md:98-116`
- Modify: `README.md:55-80`

**Interfaces:**
- Consumes: runtime record field `session: str`, delivery record, and `subprocess.run`.
- Produces: `command_teardown(...)` removes the verified worktree and its tmux session; documentation specifies its point in the task lifecycle.

- [ ] **Step 1: Write failing tests for teardown session cleanup**

Add two focused tests after `test_record_delivery_marks_landed_work_for_teardown`. Patch `KANBAN.git_output` to return an empty status and patch `KANBAN.subprocess.run` to record calls. The first test sets a landed delivery record, calls `command_teardown`, and asserts calls include both:

```python
["git", "worktree", "remove", str(worktree)]
["tmux", "kill-session", "-t", "task-graph-first-plan-run-a-001-work"]
```

The second test makes the `tmux kill-session` call return a nonzero `CompletedProcess` with a “can't find session” error and asserts `command_teardown` does not raise. Use a temporary existing `worktree` path from the runtime record so the mocked Git status command receives the expected path.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_kanban.KanbanTest.test_teardown_kills_recorded_tmux_session tests.test_kanban.KanbanTest.test_teardown_allows_already_absent_tmux_session
```

Expected: FAIL because `command_teardown` never calls `tmux kill-session`.

- [ ] **Step 3: Add minimal tmux-session cleanup**

After a successful `git worktree remove`, invoke:

```python
tmux = subprocess.run(
    ["tmux", "kill-session", "-t", str(record["session"])],
    cwd=repo,
    text=True,
    capture_output=True,
)
if tmux.returncode and "can't find session" not in tmux.stderr.lower():
    raise SystemExit(tmux.stderr.strip() or tmux.stdout.strip() or "tmux failed to remove session")
```

Keep the existing guard checks before either removal. Do not kill a session if worktree cleanup fails.

- [ ] **Step 4: Document the precise cleanup point**

In `SKILL.md`, say that `DONE` is review-ready only; retain the task tmux session and worktree through review, task-commit creation, integration, and verification. State that after `record-delivery --result landed`, run teardown to remove both the worktree and its recorded tmux session before moving that task to `done`. State that this does not wait for the final PR.

In `README.md`, add the same sequence to Guarded Delivery and describe `teardown` as removal of both resources. Retain failed/retrying workers for diagnosis and require explicit discard for abandoned work.

- [ ] **Step 5: Run focused tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_kanban.KanbanTest.test_teardown_refuses_unlanded_work_without_explicit_discard tests.test_kanban.KanbanTest.test_teardown_kills_recorded_tmux_session tests.test_kanban.KanbanTest.test_teardown_allows_already_absent_tmux_session tests.test_kanban.KanbanTest.test_record_delivery_marks_landed_work_for_teardown
```

Expected: PASS.

- [ ] **Step 6: Run full verification**

Run:

```bash
python3 -m unittest discover
```

Expected: PASS with no failures.

- [ ] **Step 7: Commit**

```bash
git add SKILL.md README.md scripts/kanban.py tests/test_kanban.py docs/superpowers/plans/2026-07-15-task-worker-teardown.md
git commit -m "feat: clean up landed task tmux sessions"
```
