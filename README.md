# Task Graph

> Turn implementation plans into dependency-aware task graphs for parallel, context-isolated AI coding agents.

Task Graph is a skill for Codex and Claude Code that helps agents execute large implementation plans without losing control of scope, context, or task order.

Instead of asking one long-running agent to hold the entire plan in context, Task Graph converts an approved implementation plan into small, explicit task files. Each task declares its scope, dependencies, acceptance criteria, and test notes. The helper script then identifies which tasks are unblocked and can safely run in parallel.

```text
Implementation Plan
       ↓
Task Decomposition
       ↓
Dependency Graph
       ↓
Parallel Scheduling
       ↓
Isolated Agent Execution
```

## Why Use Task Graph?

Large coding-agent runs tend to fail in predictable ways:

- The agent carries too much stale context.
- Independent work is executed sequentially.
- Dependent work starts too early.
- Parallel agents conflict because ownership is unclear.
- The original implementation plan turns into informal chat history.

Task Graph makes the execution boundary explicit: first write or approve the plan, then decompose it into a graph of executable tasks, then run only the tasks whose dependencies are satisfied. Each agent owns exactly one task and reads only the context it needs.

## What It Does

Task Graph manages project-local agent work in a separate `.agent/<plan-slug>/` directory for each implementation plan.

It helps you:

- Convert an implementation plan into small markdown task files.
- Declare task dependencies explicitly.
- Identify which tasks are startable.
- Schedule independent tasks in parallel.
- Reserve launch batches and track them in a durable run ledger.
- Keep each agent focused on one task.
- Track progress through `todo`, `in-progress`, and `done`.
- Regenerate a kanban-style board from filesystem state.

The workflow is intentionally conservative. A task is only startable when all dependencies are already done. Parallel execution is allowed only when tasks are independently unblocked.

## Worktree Isolation

When Task Graph launches subagents, each subagent should work in its own Git worktree on its own task branch. The main agent keeps one integration branch for the full implementation plan, then merges or cherry-picks completed task branches back into that integration branch.

By default, one approved implementation plan integrates into one feature branch. After final review and verification, Task Graph should report the branch, commits, verification results, and review notes, then ask whether you want a GitHub PR created. It should not create a PR unless you explicitly confirm. Task branches are temporary, reviewable artifacts for isolated agent work; they do not each become a GitHub PR unless you explicitly ask for that or the tasks are independently shippable.

The main agent owns `.agent/<plan-slug>/` task state during this flow. It derives and announces the short lowercase kebab-case plan slug from the approved plan, then passes it to every helper command. It moves launched tasks to `in-progress` before creating task worktrees. Subagents should not move files under the selected plan directory or rewrite its board. For unattended `codex exec` tasks, workers edit and test only in their task worktree, return their complete report in the final response, and do not commit; the controller persists that response, reviews the diff, and creates the task-branch commit. After the main agent integrates and verifies a task branch, it moves that task to `done` and regenerates the board.

## Durable Runs

Task Graph stores per-run coordination files under `.agent/<plan-slug>/runs/<run-id>/`:

```text
.agent/<plan-slug>/runs/<run-id>/
  progress.md
  briefs/
  reports/
  reviews/
  diffs/
```

The run ledger lets the controller recover after compaction or restart. A task marked complete in `progress.md` is treated as done for launch purposes and is not reserved again.

Subagents use file handoffs rather than long pasted context. The controller writes a task brief and persists each unattended worker's final response as its report; reviews can use the report plus a focused diff package. Subagents report one of `DONE`, `DONE_WITH_CONCERNS`, `NEEDS_CONTEXT`, or `BLOCKED`; the controller reviews and integrates only after the status is resolved.

## Execution Modes

Every `$task-graph start` requires an explicit execution-mode selection before Task Graph reserves or launches a batch. It explains all three choices every time. There is no default mode: if no selection is supplied, Task Graph does not reserve tasks, create worktrees, write launch runtime records, or begin execution. Naming a mode in the start request is an explicit selection.

- **Managed workers:** in-session subagents, each in an isolated Git worktree and task branch.
- **Unattended `codex exec`:** non-interactive local CLI workers, one per reserved task, running in tmux. tmux is required; the local machine or remote host must remain awake. `launch-exec` records a durable per-task runtime JSON record before execution and preserves the exited pane for diagnosis. The record includes the tmux session, pane PID, command, branch, worktree, brief/report/log paths, start/finish timestamps, and exit result. It uses workspace-write access and the installed CLI's execution policy, never an automatic unsandboxed bypass.
- **Cloud delegation:** supported remote task execution. Record its remote task identifier and result location; never silently fall back to local execution.

`codex exec` is not laptop-independent: a local process still needs an awake machine or remote host. Cloud delegation is the mode for work that must continue after the local machine is unavailable.

## Guarded delivery policy

Every run records one delivery mode when it is reserved: `no-mistakes` for the full project validation pipeline and PR delivery, `direct-pr` for verified PR delivery without that pipeline, or `local-only` for a clean local fast-forward. Add `+yolo` only when you want the controller to complete the routine green merge or fast-forward for that run. +yolo never permits a red merge, a security-sensitive or irreversible action, or an explicit discard.

Before an unattended worker launches, Task Graph verifies that its directory is a registered Git worktree root on the task branch and must not be the controller checkout. Its runtime record preserves that worktree identity and base commit. Status is conservative: a recognized harness process is running, an idle shell is idle or dead, and an unrecognized process is `UNKNOWN` and must be inspected rather than relaunched automatically.

After a successful worker report, approved review, and tests, `delivery-ready` states the permitted controller action. `no-mistakes` runs the validation pipeline, `direct-pr` opens a PR, and `local-only` fast-forwards only a clean integration branch. Worktrees with uncommitted or unlanded work are never removed without an explicit discard confirmation.

## Low-intrusion local-worker monitoring

After launching an unattended local worker, the controller runs one standalone `kanban.py status ... --json` probe immediately after launch. This standalone `status --json` probe is followed by a platform-native wait of 60 seconds before every later probe, so automatic checks occur at most once per minute. Automatic polling stops when the worker status is terminal: `SUCCEEDED_AWAITING_REVIEW`, `NEEDS_ATTENTION`, `STALE`, or `UNKNOWN`.

Controller monitoring must never use shell `sleep`, compound commands, or `status --watch`; each probe is a standalone read-only status command. Approval is needed only for the standalone status-command prefix, never for an artificial delay command. This cadence applies to automatic controller monitoring only: users can still run the read-only dashboard examples below whenever they request them. In short: never use shell `sleep` for automatic monitoring.

## Faster DAG Batches

Task Graph launches the entire currently unblocked batch, up to the selected limit. It collects, reviews, integrates, and verifies that independent batch before calculating the next one, so dependents start immediately after their prerequisites are proven. During task creation, tightly coupled linear work should be coalesced into one bounded task; use separate task files only for real parallelism or independently reviewable milestones.

## Portable diff packages

Before integrating each completed `ship` task, archive its task-branch delta with the helper:

```bash
python3 <skill-dir>/scripts/kanban.py archive-diff --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --base <base-commit> --head <task-head-commit> --branch <task-branch> --review reviews/001-example.md
```

The command writes a binary-capable unified patch and a concise metadata summary under `.agent/<plan-slug>/runs/<run-id>/diffs/`. The controller links both paths from the task review and completion ledger entry after integration verification.

## Improvement Loop Checkpoints

Some runs need an implementation task followed by an audit task to prove the desired outcome was reached. After each failed audit that reports `DONE_WITH_CONCERNS` because the outcome is still below the acceptance bar, Task Graph should pause before launching more work.

The controller should read the audit report, summarize the target outcome, measured result, remaining gap, listed concerns, and prior loop attempts in the same run. It should then ask whether to stop with the current unresolved result or continue into another focused improvement-and-audit loop.

This checkpoint happens after the first failed audit and every later failed audit. Passing audits continue through the normal review, integration, and verification flow.

## Project Structure

Each target project uses this layout:

```text
.agent/
  <plan-slug>/
    kanban.md
    todo/
    in-progress/
    done/
    runs/
```

Plans are intentionally isolated: the helper requires `--plan <plan-slug>` for every command and never reads or updates the legacy shared `.agent/tasks`, `.agent/kanban.md`, or `.agent/runs` layout. This allows independent task groups to use the same task names and run IDs without collision.

The skill includes a helper script:

```text
scripts/kanban.py
```

The helper can regenerate the board, inspect startable tasks, compute a parallel launch batch, move the next task into progress, and mark verified tasks as done.

It can also reserve a batch for a run, emit machine-readable planning JSON, and initialize run ledgers for restart-safe execution.

## Installation

Install without cloning the repo:

```bash
npx task-graph-skill@latest install
```

Install for both Codex and Claude Code:

```bash
./install.sh
```

Install only for Codex:

```bash
npx task-graph-skill@latest install --codex-only
# or, from a local clone:
./install.sh --codex-only
```

Install only for Claude Code:

```bash
npx task-graph-skill@latest install --claude-only
# or, from a local clone:
./install.sh --claude-only
```

For local development, symlink this repo instead of copying it:

```bash
./install.sh --link --force
```

Default install locations:

```text
Codex:       ${CODEX_HOME:-$HOME/.codex}/skills/task-graph
Claude Code: ${CLAUDE_HOME:-$HOME/.claude}/skills/task-graph
```

## Usage

Ask Codex to create tasks from an approved implementation plan:

```text
$task-graph tasks
```

Ask Codex to start dependency-safe execution. The default parallel launch limit is `5`:

```text
$task-graph start
```

Override the parallel launch limit:

```text
$task-graph start --limit 3
```

Optionally you can also call the helper directly.

Inspect the task graph:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --plan <plan-slug> --limit 5
```

Inspect the task graph as JSON:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --plan <plan-slug> --limit 5 --json
```

Reserve the next launch batch for a run:

```bash
python3 <skill-dir>/scripts/kanban.py reserve --repo <repo-root> --plan <plan-slug> --limit 5 --run-id <run-id>
```

Launch one prepared reserved task unattended (tmux is required):

```bash
python3 <skill-dir>/scripts/kanban.py launch-exec --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --branch task-graph/<plan-slug>/001-example --worktree <task-worktree>
```

Use the read-only dashboard from another tmux pane:

```bash
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root>
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --json
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root> --watch --interval 2
tmux attach -t task-graph-<plan-slug>-<run-id>-001-example
```

Start the next unblocked task:

```bash
python3 <skill-dir>/scripts/kanban.py start --repo <repo-root> --plan <plan-slug>
```

Mark a verified task as done:

```bash
python3 <skill-dir>/scripts/kanban.py done --repo <repo-root> --plan <plan-slug> --task 001-example.md
```

Regenerate the board:

```bash
python3 <skill-dir>/scripts/kanban.py board --repo <repo-root> --plan <plan-slug>
```

## Task Contract

Each generated task is designed for a fresh-context agent and should include:

- `Type` (`ship` or `scout`; omitted means `ship`)
- `Goal`
- `Context`
- `Scope`
- `Out Of Scope`
- `Dependencies`
- `Parallel`
- `Acceptance Criteria`
- `Test Notes`

The `Dependencies` section is the scheduling source of truth. `None` means the task is unblocked. The `Parallel` section is human-readable guidance; dependency parsing determines what can actually start.

`ship` tasks deliver code, tests, or docs that should be integrated into the feature branch. `scout` tasks investigate, reproduce, audit, or plan and deliver a report; they only become code work if you explicitly convert them into ship tasks.

## Example Use Case

You have an implementation plan for a backend feature that touches database schema, API handlers, validation, tests, and documentation.

A single agent could attempt the entire plan, but it may mix concerns, start work out of order, or retain irrelevant context.

Task Graph decomposes the plan into files like:

```text
001-add-database-schema.md
002-add-repository-methods.md
003-add-api-validation.md
004-add-handler-tests.md
005-update-documentation.md
```

If `003-add-api-validation.md` and `005-update-documentation.md` do not depend on each other, they can run in parallel. If `004-add-handler-tests.md` depends on the API handler work, it waits until that dependency is done.

## Design Principles

Task Graph is built around a few strict rules:

- Plans are approved before execution.
- Tasks are explicit files, not hidden chat state.
- Dependencies are declared in markdown and parsed by the helper.
- Agents own one task at a time.
- Subagents work in isolated Git worktrees and task branches.
- Subagents communicate through task briefs, report files, and status values.
- The main agent integrates task branches into one feature branch by default.
- The main agent owns kanban state after integration.
- The run ledger is the recovery source after context loss.
- Context is intentionally limited.
- Work moves to done only after verification.
- The kanban board is generated from filesystem state.

## Why This Exists

AI coding agents are getting better at implementation, but large plans still need execution discipline.

Task Graph provides that discipline. It turns a plan into a dependency graph, schedules safe parallel work, and gives each agent a clean context boundary.

Think of it as a lightweight execution layer for AI coding agents.
