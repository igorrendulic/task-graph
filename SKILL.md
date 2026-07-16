---
name: task-graph
description: Turn approved implementation plans into plan-isolated task files and a conservative executable DAG in .agent/<plan-slug>/.
---

# Task Graph

## Board Paths

Assume the repository root is the current working directory unless the user gives another path.

- Canonical DAG: `.agent/<plan-slug>/dag.json`
- Board: `.agent/<plan-slug>/kanban.md`
- Task folders: `.agent/<plan-slug>/todo`, `.agent/<plan-slug>/in-progress`, `.agent/<plan-slug>/done`

For every plan, read the supplied implementation plan and derive and announce a concise lowercase kebab-case `<plan-slug>` from its goal. Reuse that slug when resuming the same plan. The target repository must contain `.agent/`; if it does not, ask before creating project workflow files. Do not use or update a legacy shared `.agent/tasks`, `.agent/kanban.md`, or `.agent/runs` layout.

## `tasks` workflow

Use this workflow when the user asks to turn an approved plan into implementation tasks.

Before creating or refreshing task files, `kanban.md`, or `dag.json`, read [the DAG-generation reference](references/dag-generation.md) completely and follow it. It defines the task-file contract, canonical DAG schema, conservative scheduling rules, validation, write order, and the v1 planning-only boundary.
