Here’s a more sellable README version:

Task Graph Skill

Turn implementation plans into dependency-aware task graphs for parallel, context-isolated AI coding agents.

Task Graph is a skill for Codex and Claude Code that helps agents execute large implementation plans without losing control of scope, context, or task order.

Instead of asking one long-running agent to hold the entire plan in context, Task Graph converts an approved implementation plan into small, explicit task files. Each task declares its scope, dependencies, acceptance criteria, and test notes. The helper script then identifies which tasks are unblocked and can safely run in parallel.

Implementation Plan
       ↓
Task Decomposition
       ↓
Dependency Graph
       ↓
Parallel Scheduling
       ↓
Isolated Agent Execution

Why Use Task Graph?

Large coding-agent runs tend to fail in predictable ways:

* The agent carries too much stale context.
* Independent work is executed sequentially.
* Dependent work starts too early.
* Parallel agents conflict because ownership is unclear.
* The original implementation plan turns into informal chat history.

Task Graph makes the execution boundary explicit.

First, write or approve the plan. Then decompose it into a graph of executable tasks. Then run only the tasks whose dependencies are satisfied, with each agent owning exactly one task and reading only the context it needs.

What It Does

Task Graph manages project-local agent work inside .agent/.

It helps you:

* Convert an implementation plan into small markdown task files.
* Declare task dependencies explicitly.
* Identify which tasks are startable.
* Schedule independent tasks in parallel.
* Keep each agent focused on one task.
* Track progress through todo, in-progress, and done.
* Regenerate a kanban-style board from the filesystem state.

The workflow is intentionally conservative. A task is only startable when all dependencies are already done. Parallel execution is allowed only when tasks are independently unblocked.

Project Structure

Each target project uses this layout:

.agent/
  kanban.md
  tasks/
    todo/
    in-progress/
    done/

The skill includes a helper script:

scripts/kanban.py

The helper can:

* regenerate the board
* inspect startable tasks
* compute a parallel launch batch
* move the next task into progress
* mark verified tasks as done

Installation

Install for both Codex and Claude Code:

./install.sh

Install only for Codex:

./install.sh --codex-only

Install only for Claude Code:

./install.sh --claude-only

For local development, symlink this repo instead of copying it:

./install.sh --link --force

Default install locations:

Codex:       ${CODEX_HOME:-$HOME/.codex}/skills/task-graph
Claude Code: ${CLAUDE_HOME:-$HOME/.claude}/skills/task-graph

Usage

Create tasks from an approved implementation plan:

Use $task-graph tasks to create implementation tasks from this plan.

Start dependency-safe execution:

Use `$task-graph start` to begin execution with the default parallel launch limit of 5.

Use a different parallel limit:

Use `$task-graph start --limit 3`.

You can also call the helper directly.

Optionally inspect the task graph (or manually navigate to .agent/kanban.md):

python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --limit 5

Start the next unblocked task:

python3 <skill-dir>/scripts/kanban.py start --repo <repo-root>

Mark a verified task as done:

python3 <skill-dir>/scripts/kanban.py done --repo <repo-root> --task 001-example.md

Regenerate the board:

python3 <skill-dir>/scripts/kanban.py board --repo <repo-root>

Task Contract

Each generated task is designed for a fresh-context agent and should include:

* Goal
* Context
* Scope
* Out Of Scope
* Dependencies
* Parallel
* Acceptance Criteria
* Test Notes

The Dependencies section is the scheduling source of truth. None means the task is unblocked. The Parallel section is human-readable guidance; dependency parsing determines what can actually start.

Example Use Case

You have an implementation plan for a backend feature that touches database schema, API handlers, validation, tests, and documentation.

A single agent could attempt the entire plan, but it may mix concerns, start work out of order, or retain irrelevant context.

Task Graph decomposes the plan into files like:

001-add-database-schema.md
002-add-repository-methods.md
003-add-api-validation.md
004-add-handler-tests.md
005-update-documentation.md

If 003-add-api-validation.md and 005-update-documentation.md do not depend on each other, they can run in parallel. If 004-add-handler-tests.md depends on the API handler work, it waits until that dependency is done.

Design Principles

Task Graph is built around a few strict rules:

* Plans are approved before execution.
* Tasks are explicit files, not hidden chat state.
* Dependencies are declared in markdown and parsed by the helper.
* Agents own one task at a time.
* Context is intentionally limited.
* Work moves to done only after verification.
* The kanban board is generated from filesystem state.

Why This Exists

AI coding agents are getting better at implementation, but large plans still need execution discipline.

Task Graph provides that discipline. It turns a plan into a dependency graph, schedules safe parallel work, and gives each agent a clean context boundary.

Think of it as a lightweight execution layer for AI coding agents.