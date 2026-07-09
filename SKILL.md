---
name: task-graph
description: Manage project-local agent task graphs and kanban workflow in .agent/kanban.md and .agent/tasks/{todo,in-progress,done}. Use when the user asks to create implementation tasks, plan task files, start kanban implementation, pick the next task, move task files between todo/in-progress/done, or coordinate parallelizable project work with clean-context Codex runs.
---

# Task Graph

## Board Paths

Assume the repository root is the current working directory unless the user gives another path.

- Board: `.agent/kanban.md`
- Task folders: `.agent/tasks/todo`, `.agent/tasks/in-progress`, `.agent/tasks/done`
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
2. Run the helper `plan` command to determine which TODO tasks are startable in parallel and which must remain sequential or blocked:
   - A task is startable only when every dependency named in `Dependencies` is already in `done`.
   - Tasks can run in parallel when they are startable and neither task depends on the other.
   - Tasks with dependencies still in `todo` or `in-progress` are sequential or blocked and must not be launched.
   - Use a default parallel launch limit of `5` unless the user gives a different limit.
3. Establish an integration branch for the overall implementation plan before launching work:
   - Use the current branch when it is already the intended feature branch.
   - Otherwise create or switch to a feature branch for the plan, for example `task-graph/<plan-slug>`.
   - The integration branch is the only branch that should become the final GitHub PR by default.
4. Move each task in the launch batch from `.agent/tasks/todo/` to `.agent/tasks/in-progress/` on the integration branch, then regenerate `.agent/kanban.md`.
5. If more than one task is recommended in the launch batch, spawn worker agents for those tasks by default. Do not wait for the user to explicitly ask for parallel agents.
6. For every spawned worker agent, create a dedicated Git worktree and task branch from the integration branch:
   - Use a branch name that includes the task prefix and slug, for example `task-graph/<plan-slug>/001-add-schema`.
   - Use a unique worktree path outside the main checkout or in a repo-ignored worktree directory.
   - Never launch two worker agents in the same checkout.
7. For each worker agent, use this prompt shape:
   - You are working in a dedicated Git worktree on a dedicated task branch. Do not switch branches or edit another agent's worktree.
   - Own exactly one task file: `<task-file>`.
   - Do not move `.agent/tasks/...` files and do not regenerate `.agent/kanban.md`; the main agent owns kanban state after integration.
   - Read only that task file, this skill, done task artifacts if needed, and the minimum code required for the task.
   - Implement only the task's `Scope`; respect `Out Of Scope`.
   - Run the narrowest useful tests first, then broader tests when appropriate.
   - Commit the task's code, tests, and documentation changes on the task branch.
   - In the final response, list the task branch, worktree path, commit SHA, changed files, test commands, and any known conflicts or follow-up needed.
8. If only one task is recommended, prefer the same worktree and task-branch flow unless the user explicitly asks for local in-checkout execution.
9. Before implementing a task locally, clear working context in practice:
   - Read only the selected task file, this skill, and the minimum code needed for that task.
   - Do not carry assumptions from previously completed tasks unless they are present in code, the selected task, or done task artifacts.
10. Implement only the selected task's `Scope`.
11. Run the narrowest useful tests first, then broader tests when appropriate.
12. Integrate completed task branches back into the integration branch:
   - Merge or cherry-pick completed task branch commits in dependency order.
   - Resolve conflicts on the integration branch, not inside unrelated task worktrees.
   - Run the relevant verification after integrating each task branch, or after the batch when tasks are genuinely independent.
13. Move task files through `.agent/tasks/...` only from the integration branch:
   - Move the task to `done` only after its task branch is integrated and verification passes.
   - Regenerate `.agent/kanban.md` after task-state changes.
14. Create one final GitHub PR from the integration branch by default. Create separate PRs per task branch only when the user explicitly asks or the tasks are independently shippable.

Use the helper to plan parallel work without moving files:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --limit 5
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
- `start` selects the first startable todo task by filename, moves it to `in-progress`, rewrites the board, and prints the task path plus possible parallel candidates.
- `done --task <file>` moves a matching in-progress task to `done` and rewrites the board.
- Dependencies are parsed from the `## Dependencies` section as task filenames when present. `None` means no blocker.
- The `## Parallel` section is human guidance. Dependency parsing is authoritative for helper decisions.

If the helper cannot confidently parse a dependency or choose a task, inspect the task files and update them before moving anything.
