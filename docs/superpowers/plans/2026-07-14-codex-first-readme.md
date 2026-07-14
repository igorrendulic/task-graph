# Codex-First README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the public README into a Codex-first guided overview of Task Graph’s guarded worker lifecycle.

**Architecture:** `README.md` moves from topic-by-topic reference prose to a narrative product flow. `tests/test_skill_docs.py` protects the ordering and the behavioral statements that users depend on.

**Tech Stack:** Markdown, Python `unittest`.

## Global Constraints

- Codex quick start appears before Claude Code installation variants.
- Preserve plan-scoped paths, all helper commands, and the existing monitoring contract.
- Describe `+yolo` as green-only routine delivery; it never authorizes failed verification, security-sensitive work, irreversible work, or discard.
- Keep FirstMate terminology and functionality out of the Task Graph README.

---

### Task 1: Lock the public README structure with a documentation contract

**Files:**
- Modify: `tests/test_skill_docs.py`

**Interfaces:**
- Produces assertions that `README.md` has `What it is`, `Features`, `Quick Start`, `How It Works`, `Guarded Delivery`, and `Command Reference` in that order.

- [ ] **Step 1: Write the failing README-order test**

```python
def test_readme_is_codex_first_guided_reference(self) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = ["## What it is", "## Features", "## Quick Start", "## How It Works", "## Guarded Delivery", "## Command Reference"]
    for heading in headings:
        self.assertIn(heading, readme)
    self.assertLess(readme.index("## Quick Start"), readme.index("Install for Claude Code"))
    self.assertLess(readme.index("## Guarded Delivery"), readme.index("## Command Reference"))
```

- [ ] **Step 2: Run the new test and confirm it fails for missing headings**

Run: `python3 -m unittest tests.test_skill_docs.SkillDocsTest.test_readme_is_codex_first_guided_reference`

Expected: FAIL because the old README has no `What it is` heading.

- [ ] **Step 3: Commit the failing-contract checkpoint only if the repository convention permits red commits; otherwise leave it staged while completing Task 2**

```bash
git add tests/test_skill_docs.py
git commit -m "test: define Codex-first readme contract"
```

### Task 2: Rewrite the README around the worker lifecycle

**Files:**
- Modify: `README.md`
- Modify: `tests/test_skill_docs.py`

**Interfaces:**
- Consumes the ordered headings asserted in Task 1.
- Produces a self-contained public guide and retains all documented helper commands.

- [ ] **Step 1: Replace the opening with the approved sections**

Use this exact high-level order:

```markdown
# Task Graph

> Turn an approved implementation plan into a safe, dependency-aware crew of coding workers.

## What it is
## Features
## Quick Start
## How It Works
## Guarded Delivery
## Low-Intrusion Monitoring
## Command Reference
## Installation and Other Harnesses
## Task Contract
## Example Use Case
## Contributing
## License
```

Under `Quick Start`, show `npx task-graph-skill@latest install`, `$task-graph tasks`, and `$task-graph start`; explain that the start flow asks for execution mode and records `--delivery-mode direct-pr` before workers launch.

- [ ] **Step 2: Add the lifecycle flow and safety copy**

Include a compact text flow:

```text
Approved plan → task files → dependency-safe batch → dedicated worktrees
→ worker reports → review and verification → delivery policy → durable record
```

Document all three delivery modes, `+yolo` limits, controller-checkout refusal, conservative `UNKNOWN` liveness, `delivery-ready`, `record-delivery --result landed`, explicit discard, the immediate JSON probe, native 60-second waits, and terminal polling states.

- [ ] **Step 3: Move the complete helper examples under Command Reference**

Keep direct helper examples for `plan`, `reserve` with `--delivery-mode`, `launch-exec`, `status`, `delivery-ready`, `record-delivery`, `teardown`, `done`, and `board`. Retain `status --watch` as a user-requested dashboard command and state it is not controller automation.

- [ ] **Step 4: Run the documentation-contract tests**

Run: `python3 -m unittest tests/test_skill_docs.py`

Expected: PASS.

- [ ] **Step 5: Run the complete suite and whitespace check**

Run: `python3 -m unittest discover && git diff --check`

Expected: all tests pass and no whitespace errors.

- [ ] **Step 6: Commit**

```bash
git add README.md tests/test_skill_docs.py
git commit -m "docs: reorganize Codex-first readme"
```
