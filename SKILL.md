---
name: task-graph
description: Manage project-local agent task graphs and kanban workflow in .agent/kanban.md and .agent/tasks/{todo,in-progress,done}. Use when the user asks to create implementation tasks, plan task files, start kanban implementation, pick the next task, move task files between todo/in-progress/done, or coordinate parallelizable project work with clean-context Codex runs.
---

# Task Graph

## Board Paths

Assume the repository root is the current working directory unless the user gives another path.

- Board: `.agent/kanban.md`
- Task folders: `.agent/tasks/todo`, `.agent/tasks/in-progress`, `.agent/tasks/done`
- Run artifacts: `.agent/runs/<run-id>/`
- Helper: `scripts/kanban.py`

Run helper commands from any directory, passing `--repo <repo-root>` when the current directory is not the target repo. The target repo must contain `.agent/`; if it does not, ask before creating project workflow files.

## Commands

### tasks command

Use this workflow when the user asks to create an implementation plan or task breakdown.

1. Inspect the requested feature and the relevant project code, docs, tests, and conventions.
2. Decompose work into small implementation tasks that can be completed from a fresh context.
3. Assign stable numeric prefixes (`001-...md`, `002-...md`) and concise slug names.
4. Mark parallelization explicitly in each task:
   - `Dependencies`: name prerequisite task files, or `None`.
   - `Parallel`: say whether the task can run in parallel and with which task files.
5. Write each task with these sections:
   - `Type` (`ship` for implementation work, or `scout` for investigation/report-only work; default to `ship` when omitted)
   - `Goal`
   - `Context`
   - `Scope`
   - `Out Of Scope`
   - `Dependencies`
   - `Parallel`
   - `Acceptance Criteria`
   - `Test Notes`
6. Keep each task scoped tightly enough for a fresh-context agent, and include project-specific commands or tests discovered from the repo in `Test Notes`.
7. Put new task files in `.agent/tasks/todo/`.
8. Regenerate `.agent/kanban.md` so TODO, IN PROGRESS, and DONE match the filesystem.

Use the helper after writing task files:

```bash
python3 <skill-dir>/scripts/kanban.py board --repo <repo-root>
```

## start command

Use this workflow when the user asks to start implementation.

1. Read `.agent/kanban.md` and the task files.
2. Choose a stable run id for this execution, for example `<plan-slug>-YYYYMMDD`, and inspect `.agent/runs/<run-id>/progress.md` if it already exists:
   - Treat tasks marked `complete` in the ledger as already done, even if conversational context was lost.
   - Reconcile any ledger/task-board mismatch before launching new work.
3. Run the helper `plan` command to determine which TODO tasks are startable in parallel and which must remain sequential or blocked:
   - A task is startable only when every dependency named in `Dependencies` is already in `done`.
   - Tasks can run in parallel when they are startable and neither task depends on the other.
   - Tasks with dependencies still in `todo` or `in-progress` are sequential or blocked and must not be launched.
   - Use a default parallel launch limit of `5` unless the user gives a different limit.
   - Use `plan --json` when machine-readable output is useful for spawning or bookkeeping.
4. Establish an integration branch for the overall implementation plan before launching work:
   - Use the current branch when it is already the intended feature branch.
   - Otherwise create or switch to a feature branch for the plan, for example `task-graph/<plan-slug>`.
   - The integration branch is the only branch that should become the final GitHub PR by default.
5. Reserve the launch batch on the integration branch:
   - Run `reserve --run-id <run-id> --limit <n>` to move startable tasks to `in-progress`, regenerate `.agent/kanban.md`, and initialize `.agent/runs/<run-id>/`.
   - Keep task briefs in `.agent/runs/<run-id>/briefs/`, subagent reports in `.agent/runs/<run-id>/reports/`, and review notes or diff packages in `.agent/runs/<run-id>/reviews/`.
6. If more than one task is recommended in the launch batch, spawn worker agents for those tasks by default. Do not wait for the user to explicitly ask for parallel agents.
7. For every spawned worker agent, create a dedicated Git worktree and task branch from the integration branch:
   - Detect whether the controller is already in a linked worktree before creating more worktrees.
   - Prefer platform-native worktree/session tooling when it exists.
   - Use project-local `.worktrees/` or `worktrees/` only when that directory is ignored by git; otherwise use an external temp/worktree location.
   - Use a branch name that includes the task prefix and slug, for example `task-graph/<plan-slug>/001-add-schema`.
   - Never launch two worker agents in the same checkout.
   - Record the task branch, worktree path, base commit, and eventual head commit in the run ledger.
   - Never remove a worktree that contains unintegrated, unpushed, or otherwise unlanded work unless the user explicitly confirms discard.
8. For each worker agent, use this prompt shape:
   - You are working in a dedicated Git worktree on a dedicated task branch. Do not switch branches or edit another agent's worktree.
   - Own exactly one task file: `<task-file>`.
   - Read the task brief file first: `.agent/runs/<run-id>/briefs/<task-file>`.
   - Do not move `.agent/tasks/...` files and do not regenerate `.agent/kanban.md`; the main agent owns kanban state after integration.
   - Read only the task brief, this skill, done task artifacts if needed, and the minimum code required for the task.
   - Implement only the task's `Scope`; respect `Out Of Scope`.
   - Run the narrowest useful tests first, then broader tests when appropriate.
   - Commit the task's code, tests, and documentation changes on the task branch.
   - Write the full report to `.agent/runs/<run-id>/reports/<task-file>`.
   - Reply with only status, task branch, worktree path, commit SHA, one-line test summary, concerns, and report path.
9. Subagents must report one of these statuses:
   - `DONE`: implementation is complete and ready for review.
   - `DONE_WITH_CONCERNS`: implementation is complete, but the report lists correctness, scope, or maintainability concerns.
   - `NEEDS_CONTEXT`: the subagent needs specific missing information before continuing.
   - `BLOCKED`: the task cannot be completed as scoped.
10. Handle subagent statuses before integration:
   - For `DONE`, create a task diff/review package and run a task-scoped review for spec compliance and code quality.
   - For `DONE_WITH_CONCERNS`, read the concerns and decide whether to review, dispatch a fix, or escalate before integration.
   - For `NEEDS_CONTEXT`, provide the missing context and re-dispatch the same task.
   - For `BLOCKED`, either provide context, use a more capable agent, split the task, or escalate to the user.
11. If a task's `Type` is `scout`, capture its report in the run directory and mark it done after review; do not integrate code unless the user explicitly converts it into ship work.
12. If only one task is recommended, prefer the same worktree and task-branch flow unless the user explicitly asks for local in-checkout execution.
13. Before implementing a task locally, clear working context in practice:
   - Read only the selected task file, this skill, and the minimum code needed for that task.
   - Do not carry assumptions from previously completed tasks unless they are present in code, the selected task, or done task artifacts.
14. Implement only the selected task's `Scope`.
15. Run the narrowest useful tests first, then broader tests when appropriate.
16. Integrate completed and reviewed task branches back into the integration branch:
   - Merge or cherry-pick completed task branch commits in dependency order.
   - Resolve conflicts on the integration branch, not inside unrelated task worktrees.
   - Run the relevant verification after integrating each task branch, or after the batch when tasks are genuinely independent.
17. Move task files through `.agent/tasks/...` only from the integration branch:
   - Move the task to `done` only after its task branch is integrated and verification passes.
   - Regenerate `.agent/kanban.md` after task-state changes.
   - Append a `complete` entry to `.agent/runs/<run-id>/progress.md` with the relevant commits and review result.
18. After all ship tasks are integrated, run a final whole-branch review before creating the final PR.
19. Create one final GitHub PR from the integration branch by default. Create separate PRs per task branch only when the user explicitly asks or the tasks are independently shippable.

Use the helper to plan parallel work without moving files:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --limit 5
```

Use JSON output for scripted launch bookkeeping:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --limit 5 --json
```

Reserve a launch batch and initialize the run ledger:

```bash
python3 <skill-dir>/scripts/kanban.py reserve --repo <repo-root> --limit 5 --run-id <run-id>
```

Use the helper to start the next task:

```bash
python3 <skill-dir>/scripts/kanban.py start --repo <repo-root>
```

Use the helper to finish a task after verification:

```bash
python3 <skill-dir>/scripts/kanban.py done --repo <repo-root> --task 001-example.md
```

## Helper Behavior

The helper is intentionally conservative:

- `board` rewrites `.agent/kanban.md` from files present in `todo`, `in-progress`, and `done`.
- `plan --limit <n>` prints the recommended parallel launch batch, additional startable tasks, and sequential or blocked tasks without moving files.
- `plan --json --limit <n>` prints the same scheduling decision as structured JSON.
- `reserve --limit <n> --run-id <id>` moves the recommended launch batch to `in-progress`, rewrites the board, and initializes `.agent/runs/<id>/progress.md`.
- `start` selects the first startable todo task by filename, moves it to `in-progress`, rewrites the board, and prints the task path plus possible parallel candidates.
- `done --task <file>` moves a matching in-progress task to `done` and rewrites the board.
- Dependencies are parsed from the `## Dependencies` section as task filenames when present. `None` means no blocker.
- Task type is parsed from `## Type`; supported values are `ship` and `scout`, and omitted or unknown values default to `ship`.
- The `## Parallel` section is human guidance. Dependency parsing is authoritative for helper decisions.
- The run ledger is restart guidance. Tasks marked `complete` in `.agent/runs/<id>/progress.md` are not relaunched by `reserve`.

If the helper cannot confidently parse a dependency or choose a task, inspect the task files and update them before moving anything.
