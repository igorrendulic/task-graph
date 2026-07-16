# Task Graph README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dense README with a Codex-first guide that explains Task Graph, its installation, first workflow, monitoring, and core operating concepts.

**Architecture:** Keep `README.md` as a concise public guide and retain detailed operational behavior in `SKILL.md`. The README will begin with an install-and-use path, then explain only the operational concepts needed to choose and monitor a run. A checked real tmux screenshot may supplement the monitoring section; it is omitted when no safe, legible session is available.

**Tech Stack:** Markdown, Node.js installer, Python unit tests, tmux (optional screenshot source).

## Global Constraints

- Position Codex as the primary installation and usage path; include Claude Code only as an alternative.
- Keep every documented command consistent with `bin/task-graph-skill.js`, `scripts/kanban.py`, and `scripts/controller.py`.
- Never publish session content unless it is demonstrably safe to share and readable at README scale.
- Do not change runtime behavior, installers, or `SKILL.md`.

---

### Task 1: Rewrite the public README

**Files:**
- Modify: `README.md`
- Create (conditional): `docs/images/task-graph-tmux-session.png`
- Test: `tests/test_skill_docs.py`

**Interfaces:**
- Consumes: `bin/task-graph-skill.js` install options; `scripts/controller.py` controller session output; `scripts/kanban.py` task graph command interface.
- Produces: A standalone Codex-first README that links readers from installation to `$task-graph tasks`, `$task-graph start`, and tmux monitoring.

- [ ] **Step 1: Record the existing documentation contract**

Run: `python3 -m unittest tests.test_skill_docs -v`

Expected: The repository's documentation contract tests pass before the README edit.

- [ ] **Step 2: Rewrite the README around the first successful workflow**

Replace the current controller-first narrative with the following section order:

```markdown
# Task Graph

> Turn an approved implementation plan into small, dependency-aware coding tasks that can be run and recovered safely.

## When to use it

## Quick start (Codex)

## What happens next

## Monitor a run

## Core concepts

## Common commands

## Other installation options

## Contributing

## License
```

Include the verified Codex installation command, `$task-graph tasks`, `$task-graph start`, the controller start command, and the controller's `tmux attach -t task-graph-controller-<plan-slug>` connection command. Explain that Task Graph asks for an execution mode before reserving work.

- [ ] **Step 3: Decide whether to add a screenshot**

Inspect a current Task Graph tmux session with:

```bash
tmux list-sessions
tmux capture-pane -p -t <safe-session> -S -120
```

Add `docs/images/task-graph-tmux-session.png` and embed it only when the captured pane contains non-sensitive, readable Task Graph status information. Otherwise, omit the image and retain the command example. Do not create an artificial terminal image.

- [ ] **Step 4: Run the documentation contract tests**

Run: `python3 -m unittest tests.test_skill_docs -v`

Expected: All documentation contract tests pass after the README rewrite.

- [ ] **Step 5: Review the Markdown and command consistency**

Run:

```bash
git diff --check
node bin/task-graph-skill.js install --codex-only --dry-run
python3 scripts/controller.py --help
```

Expected: No whitespace errors; the dry run prints the Codex destination and `$task-graph` invocation; the controller help lists `start`, `status`, and `stop`.

- [ ] **Step 6: Commit the documentation update**

```bash
git add README.md docs/images/task-graph-tmux-session.png
git commit -m "docs: simplify Task Graph README"
```

If no safe screenshot was added, omit the nonexistent image path from `git add`.
