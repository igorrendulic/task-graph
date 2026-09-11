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

### Multi-project workspaces

For independently versioned projects under one parent folder, use the parent as
the workspace root. It need not itself be a Git checkout. Declare only the
projects Task Graph may execute in `.agent/task-graph.workspace.json`:

```json
{
  "schemaVersion": 1,
  "projects": [
    { "id": "frontend", "path": "apps/frontend" },
    { "id": "api", "path": "services/api" }
  ]
}
```

Every declared path must be a safe relative path to an independent Git root;
undeclared folders are never touched. In a workspace DAG, every task must name
one declared `project`, and `predictedPaths` are relative to that project.
Inspect all declared projects while planning. Use cross-project `dependsOn`
edges for contracts and delivery order; a dependent begins only after its
prerequisite has been integrated into that prerequisite project’s integration
worktree.

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

For a workspace, add `--workspace <workspace-root>` to every lifecycle command:

```sh
python3 scripts/task_graph_cli.py start <plan-slug> --workspace <workspace-root> --max-workers <n>
python3 scripts/task_graph_cli.py resume <plan-slug> <run-id> --workspace <workspace-root>
python3 scripts/task_graph_cli.py status <plan-slug> --workspace <workspace-root>
```

Workspace workers are isolated in their task’s project and receive each
prerequisite project’s integration worktree, project ID, and integrated commit
SHA as read-only implementation context. Independent tasks in distinct projects
may run concurrently. The run retains one feature branch and integration
worktree per used project for review.

Return the exact `tmux attach-session -t task-graph-<plan-slug>-<run-id>`
command printed by `start` to the current user so they can observe workers.
Workers run focused task tests and create one task-scoped commit; the controller
independently runs each task's frozen verification commands before integration.
It does not run a final full suite unless the plan includes a task for it.
Before `start`, every task must provide `verification.commands` (argv arrays),
or an explicit `verification.skipReason` when no automated check applies.
`predictedPaths` must be concrete project-relative files or directory prefixes
ending in `/`; unresolved or wildcard scopes must be refined before execution.

Use `resume` with the plan slug and run ID after interruption. It reconnects to
the live controller when its saved pane and PID are still valid, or starts one
replacement controller from the persisted snapshot:

```sh
python3 scripts/task_graph_cli.py resume <plan-slug> <run-id>
```

The controller records each worker's Codex `threadId` from its raw JSONL output.
If a worker pane dies without a completion sentinel, it resumes that exact
conversation once in the same branch and worktree, preserving unfinished edits
and original logs. It never uses `--last`. Live workers are left running.
If no session ID was captured, or continuation fails or is interrupted again,
the normal fresh-worktree repair policy applies. Existing runs without session
IDs can recover them from retained stdout logs; the local Codex session store
must still be available.

Integration requires exactly one non-merge commit descended from the launch
base, a clean worktree, and changes confined to `predictedPaths`. Both sides of
renames are checked; `.agent/` changes are always rejected. The controller runs
the frozen verification commands in the worker worktree with bounded timeouts,
records command output and exit codes, and rejects checks that change HEAD or
leave staged, unstaged, or untracked changes. An explicit skip reason remains
visible in the evidence and does not bypass commit, cleanliness, or scope checks.
These checks enforce the declared contract; they do not independently prove
every acceptance criterion or replace code review.

Older plans remain readable for planning but cannot start without the execution
contract. Update the canonical plan and start a fresh run. Do not edit frozen
run inputs; unverified pending integration from an old run is not accepted.

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

Workspace review and promotion are deliberately per project after the
coordinated run succeeds:

```sh
python3 scripts/task_graph_cli.py checkout <plan-slug> --run-id <run-id> --workspace <workspace-root> --project <project-id>
python3 scripts/task_graph_cli.py merge <plan-slug> --run-id <run-id> --workspace <workspace-root> --project <project-id>
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
