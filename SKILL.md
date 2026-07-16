---
name: task-graph
description: Manage plan-isolated project-local agent task graphs and kanban workflow in .agent/<plan-slug>/. Use when the user asks to create implementation tasks, plan task files, start kanban implementation, pick the next task, move task files between todo/in-progress/done, or coordinate parallelizable project work with clean-context Codex runs.
---

# Task Graph

## Board Paths

Assume the repository root is the current working directory unless the user gives another path.

- Board: `.agent/<plan-slug>/kanban.md`
- Task folders: `.agent/<plan-slug>/todo`, `.agent/<plan-slug>/in-progress`, `.agent/<plan-slug>/done`
- Run artifacts: `.agent/<plan-slug>/runs/<run-id>/`


For every plan, read the supplied implementation plan, derive and announce a concise lowercase kebab-case `<plan-slug>` from its goal, then pass `--plan <plan-slug>` to every helper command. Reuse that slug when resuming the same plan. Run helper commands from any directory, passing `--repo <repo-root>` when the current directory is not the target repo. The target repo must contain `.agent/`; if it does not, ask before creating project workflow files. The helper never reads or updates the legacy shared `.agent/tasks`, `.agent/kanban.md`, or `.agent/runs` layout.
