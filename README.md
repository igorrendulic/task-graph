# Task Graph Skill

Task Graph is a Codex and Claude Code skill for turning an approved implementation plan into dependency-aware task files that agents can execute with isolated context.

The core premise is:

```text
Implementation Plan (must exist before execution)
       ↓
Task Decomposition
       ↓
Dependency Graph
       ↓
Parallel Agent Scheduling
       ↓
Context-Isolated Execution
```

## What It Does

This skill manages project-local implementation work in `.agent/`. It helps an agent break a plan into small task files, track those tasks on a generated kanban board, identify which tasks can safely run in parallel, and move tasks through execution without carrying unnecessary context between them.

The workflow is intentionally conservative:

- Execution starts only after an implementation plan exists.
- Work is decomposed into explicit markdown task files.
- Each task declares dependencies and parallelization guidance.
- The helper script computes which tasks are startable from completed dependencies.
- Agents execute one task at a time with only the relevant task context.

## Project Files

The skill expects each target project to use this structure:

```text
.agent/
  kanban.md
  tasks/
    todo/
    in-progress/
    done/
```

The helper script is installed with the skill:

```text
scripts/kanban.py
```

It can regenerate the board, plan startable parallel work, start the next task, and mark a task done.

## Install

Install for both Codex and Claude Code:

```bash
./install.sh
```

Install only for Codex:

```bash
./install.sh --codex-only
```

Install only for Claude Code:

```bash
./install.sh --claude-only
```

For local development, symlink this repo instead of copying files:

```bash
./install.sh --link --force
```

By default, the installer writes to:

- Codex: `${CODEX_HOME:-$HOME/.codex}/skills/task-graph`
- Claude Code: `${CLAUDE_HOME:-$HOME/.claude}/skills/task-graph`

## Usage

Invoke the skill when you want to create or execute a task graph for a project.

For Codex:

```text
Use `$task-graph tasks` to create implementation tasks from this plan.
Use `$task-graph start` to begin dependency-safe execution with the default parallel launch limit of 5.
Use `$task-graph start --limit 3` to use a different parallel launch limit.
```

After task files exist, the helper can inspect the graph:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --limit 5
```

Start the next startable task:

```bash
python3 <skill-dir>/scripts/kanban.py start --repo <repo-root>
```

Mark a verified task done:

```bash
python3 <skill-dir>/scripts/kanban.py done --repo <repo-root> --task 001-example.md
```

Regenerate the board from task files:

```bash
python3 <skill-dir>/scripts/kanban.py board --repo <repo-root>
```

## Task Contract

Each task should be scoped tightly enough for a fresh-context agent and include:

- `Goal`
- `Context`
- `Scope`
- `Out Of Scope`
- `Dependencies`
- `Parallel`
- `Acceptance Criteria`
- `Test Notes`

Dependency parsing is based on the `Dependencies` section. `None` means the task is unblocked. The `Parallel` section is human guidance, while dependency parsing is the authoritative input for scheduling.

## Why This Exists

Large implementation plans often fail when agents carry too much context, start dependent work too early, or coordinate parallel work informally. This skill makes the plan-to-execution boundary explicit: first create the plan, then turn it into a dependency graph, then schedule only the work that can safely proceed.
