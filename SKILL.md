---
name: task-graph
description: Turn approved implementation plans into plan-isolated task files and a conservative executable DAG in .agent/<plan-slug>/.
---

# Task Graph

## When to use this skill

Use Task Graph only when an approved implementation plan is available.

- To convert that plan into task briefs, a kanban board, and a dependency-safe DAG, use `tasks`.
- To execute a validated DAG from a clean repository, use `start`.
- To continue an interrupted run, use `resume`.
- To inspect a run, use `status`.
- To inspect the completed implementation locally, use `checkout`.
- To explicitly promote a successful run, use `merge`.
- If the user has not yet approved an implementation plan, use the appropriate planning or brainstorming workflow first; do not create Task Graph artifacts.

## Board Paths

Assume the repository root is the current working directory unless the user gives another path.

- Canonical DAG: `.agent/<plan-slug>/dag.json`
- Board: `.agent/<plan-slug>/kanban.md`
- Task folders: `.agent/<plan-slug>/todo`, `.agent/<plan-slug>/in-progress`, `.agent/<plan-slug>/done`

For every plan, read the supplied implementation plan and derive and announce a concise lowercase kebab-case `<plan-slug>` from its goal. Reuse that slug when resuming the same plan. The target repository must contain `.agent/`; if it does not, ask before creating project workflow files. Do not use or update a legacy shared `.agent/tasks`, `.agent/kanban.md`, or `.agent/runs` layout.

## `tasks` workflow

Use this workflow when the user asks to turn an approved plan into implementation tasks.

Before creating or refreshing task files, `kanban.md`, or `dag.json`, read [the DAG-generation reference](references/dag-generation.md) completely and follow it. It defines the task-file contract, canonical DAG schema, conservative scheduling rules, validation, write order, and the v1 planning-only boundary.

## `start` and `resume` workflows

Use these workflows only after a validated plan DAG exists. `start` rejects a
dirty repository, snapshots the DAG and task briefs, creates an isolated feature
branch and worktrees, then starts a controller service in a plan-level tmux
session. Invoke it with:

```sh
python3 scripts/task_graph_cli.py start <plan-slug> --max-workers <n>
```

Return the exact `tmux attach-session -t task-graph-<plan-slug>-<run-id>`
command printed by `start` to the current user so they can observe workers.
Workers run focused task tests and create one task-scoped commit; the controller
does not run a final full suite in MVP.

Use `resume` with the plan slug and run ID after interruption. It reconnects to
the live controller when its saved pane and PID are still valid, or starts one
replacement controller from the persisted snapshot:

```sh
python3 scripts/task_graph_cli.py resume <plan-slug> <run-id>
```

## `status`, `checkout`, and `merge` workflows

Use `status` to inspect the newest run, or pass a run ID for a specific run:

```sh
python3 scripts/task_graph_cli.py status <plan-slug>
python3 scripts/task_graph_cli.py status <plan-slug> --run-id <run-id>
```

It reports `running`, `succeeded`, `failed`, or `already merged`. Do not infer
that a worker branch is promotable: only the run's
`task-graph/<plan-slug>/<run-id>/feature` branch can be merged. Worker-attempt
branches are never merged directly.

To inspect or run commands against a succeeded, unmerged run locally, check it
out explicitly:

```sh
python3 scripts/task_graph_cli.py checkout <plan-slug> --run-id <run-id>
```

`checkout` requires the primary checkout to be clean except for that plan's
`.agent/<plan-slug>/runs/` artifacts. It also requires the generated
integration worktree to be clean, then removes it without force so Git can
release the run feature branch to the primary checkout. It does not change run
state. The command prints the exact `git switch <base-branch>` and `merge`
commands to use after inspection.

Promotion must name the run explicitly:

```sh
python3 scripts/task_graph_cli.py merge <plan-slug> --run-id <run-id>
```

Before invoking `merge`, ensure all tasks are integrated, check out the base
branch recorded at `start`, and leave the checkout clean except for
`.agent/<plan-slug>/runs/` artifacts. The command performs a `--no-ff` merge;
on conflict it aborts safely and leaves the target branch unchanged. A completed
promotion is recorded in run state, so later attempts report `already merged`.

On macOS the controller sends one best-effort desktop alert when a run
completes. Success alerts include the exact `checkout` command first, then the
follow-up merge command after returning to the recorded base branch; failure
alerts include the status command. macOS permissions and user-session
availability can prevent delivery; Task Graph records the alert outcome and any
safe OS error in run state, which `status` displays for diagnosis. Alerts
cannot safely paste or execute terminal commands; the operator must run the
displayed command in a terminal.
