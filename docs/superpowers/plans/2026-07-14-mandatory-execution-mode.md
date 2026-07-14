# Mandatory Execution Mode Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require an explicit execution-mode choice, with a concise explanation of every mode, before Task Graph reserves or launches work.

**Architecture:** This is a documentation-contract change. `SKILL.md` defines the controller's mandatory pre-reservation gate; `README.md` presents the same operator-facing behavior; `tests/test_skill_docs.py` guards both against a future return of the implicit managed-worker default.

**Tech Stack:** Markdown, Python standard-library `unittest`.

## Global Constraints

- Every `$task-graph start` presents Managed workers, Unattended `codex exec`, and Cloud delegation with an explanation of each mode.
- A mode must be explicitly selected before task reservation, worktree creation, runtime-record creation, or execution.
- There is no implicit default; an explicit mode in the start request counts as the selection.
- Record the selected mode in the run ledger before launch.
- Do not change helper scheduling, task state transitions, worktree isolation, or `launch-exec` behavior.

---

## File Structure

- `SKILL.md` — normative execution-start procedure used by Task Graph controllers.
- `README.md` — user-facing explanation of the execution-mode choice.
- `tests/test_skill_docs.py` — static contract checks that detect missing prompt explanations or implicit-default wording.

### Task 1: Enforce the mandatory execution-mode gate in documentation

**Files:**
- Modify: `tests/test_skill_docs.py:47-59`
- Modify: `SKILL.md:65-72`
- Modify: `README.md:75-83`

**Interfaces:**
- Consumes: the approved behavior in `docs/superpowers/specs/2026-07-14-mandatory-execution-mode-design.md`.
- Produces: a controller instruction and README contract stating that start waits for an explicit mode selection and explaining every offered mode.

- [ ] **Step 1: Write the failing documentation-contract test**

Replace `test_start_requires_an_execution_mode_with_unattended_exec_contract` with this test body, retaining the existing `skill` and `readme` file reads:

```python
        self.assertIn("explicitly choose one before continuing", skill)
        self.assertIn("must not reserve tasks, create worktrees, write launch runtime records, or begin execution", skill)
        self.assertIn("`Managed workers`: in-session subagents", skill)
        self.assertIn("non-interactive local CLI workers", skill)
        self.assertIn("`Cloud delegation`: supported remote task execution", skill)
        self.assertNotIn("Managed workers (default)", skill)
        self.assertIn("requires an explicit execution-mode selection", readme)
        self.assertIn("no default mode", readme)
        self.assertIn("in-session subagents", readme)
        self.assertIn("non-interactive local CLI workers", readme)
        self.assertIn("supported remote task execution", readme)
        self.assertNotIn("Managed workers (default)", readme)
```

- [ ] **Step 2: Run the targeted test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_skill_docs.SkillDocsTest.test_start_requires_an_execution_mode_with_unattended_exec_contract -v
```

Expected: FAIL because the existing docs contain `Managed workers (default)` and do not contain the new mandatory-choice sentences.

- [ ] **Step 3: Update the controller instruction and README**

In `SKILL.md`, replace the current mode-selection paragraph and bullets in the start workflow with this exact contract:

```markdown
4. Before reserving the batch, explicitly ask the operator which execution mode they want and explain every option:
   - `Managed workers`: in-session subagents, each in an isolated Git worktree and task branch.
   - **Unattended `codex exec`**: non-interactive local CLI workers, one per reserved task, running in tmux; the local machine or remote host must remain awake.
   - `Cloud delegation`: supported remote task execution; never silently fall back to local execution.
   The operator must explicitly choose one before continuing. There is no default mode. If no selection is supplied, do not reserve tasks, create worktrees, write launch runtime records, or begin execution. An explicit mode in the start request counts as the selection. Record the selected mode in the run ledger before launching work.
```

In `README.md`, replace the opening sentence and three mode bullets in `## Execution Modes` with:

```markdown
Every `$task-graph start` requires an explicit execution-mode selection before Task Graph reserves or launches a batch. It explains all three choices every time. There is no default mode: if no selection is supplied, Task Graph does not reserve tasks, create worktrees, write launch runtime records, or begin execution. Naming a mode in the start request is an explicit selection.

- **Managed workers:** in-session subagents, each in an isolated Git worktree and task branch.
- **Unattended `codex exec`:** non-interactive local CLI workers, one per reserved task, running in tmux. tmux is required; the local machine or remote host must remain awake. `launch-exec` records a durable per-task runtime JSON record before execution and preserves the exited pane for diagnosis. The record includes the tmux session, pane PID, command, branch, worktree, brief/report/log paths, start/finish timestamps, and exit result. It uses workspace-write access and a no-prompt approval policy, never an automatic unsandboxed bypass.
- **Cloud delegation:** supported remote task execution. Record its remote task identifier and result location; never silently fall back to local execution.
```

- [ ] **Step 4: Run the targeted test to verify it passes**

Run:

```bash
python3 -m unittest tests.test_skill_docs.SkillDocsTest.test_start_requires_an_execution_mode_with_unattended_exec_contract -v
```

Expected: PASS.

- [ ] **Step 5: Run the complete test suite**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: PASS with all helper and documentation tests green.

- [ ] **Step 6: Commit the change**

```bash
git add SKILL.md README.md tests/test_skill_docs.py
git commit -m "docs: require execution mode selection"
```
