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
3. If more than one task is recommended in the launch batch, spawn worker agents for those tasks by default. Do not wait for the user to explicitly ask for parallel agents.
4. If only one task is recommended, start that task locally with the helper `start` command.
5. For each worker agent, use this prompt shape:
   - You are not alone in the codebase; other agents may be editing related files in parallel. Do not revert edits you did not make, and adjust your implementation to accommodate concurrent changes.
   - Own exactly one task file: `<task-file>`.
   - Move only that task from `.agent/tasks/todo/` to `.agent/tasks/in-progress/`, then regenerate `.agent/kanban.md`.
   - Read only that task file, this skill, done task artifacts if needed, and the minimum code required for the task.
   - Implement only the task's `Scope`; respect `Out Of Scope`.
   - Run the narrowest useful tests first, then broader tests when appropriate.
   - Move only that task from `.agent/tasks/in-progress/` to `.agent/tasks/done/` after implementation and verification, then regenerate `.agent/kanban.md`.
   - In the final response, list changed files, test commands, and any known conflicts or follow-up needed.
6. Before implementing locally, clear working context in practice:
   - Read only the selected task file, this skill, and the minimum code needed for that task.
   - Do not carry assumptions from previously completed tasks unless they are present in code, the selected task, or done task artifacts.
7. Implement only the selected task's `Scope`.
8. Run the narrowest useful tests first, then broader tests when appropriate.
9. Move the task file from `in-progress` to `done` only after implementation and verification are complete.
10. Regenerate `.agent/kanban.md`.

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
