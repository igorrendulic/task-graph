# Task Graph

> Turn an approved implementation plan into task files, a kanban board, and a conservative execution DAG.

## What it creates

Running `$task-graph tasks` writes plan-local artifacts below `.agent/<plan-slug>/`:

- `todo/*.md` contains focused, fresh-context-ready task briefs.
- `kanban.md` summarizes the task folders.
- `dag.json` is the canonical scheduling artifact.

The DAG has a `schemaVersion`, `planSlug`, and task records with stable IDs, task filenames, instructions, predicted paths and symbols, `dependsOn`, `parallelSafe`, and a scheduling rationale.

The main [skill entry point](SKILL.md) stays short as more workflows are added. The complete `tasks` contract lives in [the DAG-generation reference](references/dag-generation.md), which the skill requires agents to read before generating planning artifacts.

## Conservative scheduling

`dependsOn` is authoritative: a task can begin only after all listed task IDs are complete. `parallelSafe` is explanatory evidence, not a second scheduler.

Tasks are parallel only when their predicted edit surfaces are demonstrably disjoint. Shared files, symbols, contracts, tests, generated artifacts, or uncertain overlap are serialized. If no natural prerequisite exists, the later source-plan task depends on the earlier one. Dirty local changes that overlap planned work are called out as a clean-base requirement and make the task non-parallel-safe.

Each `tasks` run validates unique task IDs and filenames, known dependencies, self-dependencies, and graph acyclicity before it replaces the canonical `dag.json`. Rerunning the command refreshes the per-plan DAG and keeps task-file dependencies aligned with it.

## v1 scope

Task Graph v1 plans work only. It does not create feature branches, task branches, worktrees, worker sessions, merges, or pull requests. Future execution tooling can consume `dependsOn` to run and integrate tasks in dependency order.

## Execution MVP

The execution controller consumes a validated planning DAG without changing its
schema. Start a clean, committed plan with a fixed worker limit:

```bash
python3 scripts/task_graph_cli.py start <plan-slug> --max-workers 4
```

`start` snapshots `dag.json` and every resolved task brief below
`.agent/<plan-slug>/runs/<run-id>/input/`, creates a feature branch and an
integration worktree, then returns a command such as:

```bash
tmux attach-session -t task-graph-<plan-slug>-<run-id>
```

Run that command to observe the controller and worker windows. The controller
is the only process that writes state or cherry-picks worker commits. It runs
only dependency-ready tasks, uses fresh worktrees for both the first attempt and
one repair attempt, and blocks only descendants after a second failure.

State is written to `runs/<run-id>/state.json` with a run lock and durable
atomic replacement. Each attempt retains stdout, stderr, and a combined log in
`runs/<run-id>/logs/`; failed worktrees remain available for investigation.
Workers run focused tests from their task briefs and must make exactly one
non-merge commit. The controller deliberately does not run a final full suite
in this MVP.

To recover after interruption:

```bash
python3 scripts/task_graph_cli.py resume <plan-slug> <run-id>
```

`resume` uses the saved input snapshot and reconciles integration state before
scheduling. It reattaches to the live controller when possible and otherwise
starts exactly one replacement in the plan tmux session.

For an opt-in integration check of the controller itself, run:

```bash
python3 scripts/task_graph_cli.py eval-controller
```

This command creates temporary Git repositories and isolated tmux sessions. It
uses a persisted absolute scripted-worker command to verify parallel and serial
integration, retry and terminal-failure handling, and live/dead-controller
resume behavior. All temporary repositories and sessions are removed after each
scenario; the regular unit-test command below does not run these evals.

## Evaluation

Run deterministic validation and behavior-case tests with:

```bash
python3 -m unittest discover
```

Use `scripts/dag_validation.py` to validate a generated DAG and its task-file dependencies. Behavior cases under `evals/cases/` pair an agent prompt and repository fixture with explicit scheduling assertions; score an agent-produced plan directory with:

```bash
python3 scripts/dag_eval_setup.py --case evals/cases/001-disjoint
```

By default, the setup script materializes the case in a cleaned temporary repository,
runs `codex exec`, loads the generated `dag.json` into memory, validates it against
the generated task files, and removes the temporary repository before exiting.

`--repo PATH` is only for debugging a failed run; it intentionally keeps the generated repository and the path must not already exist.

The tracked `repository/` directories are templates. The setup command copies one into a new destination, initializes and commits a baseline Git repository, then applies the case's declared uncommitted changes.
