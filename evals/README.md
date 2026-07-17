# Evaluation guide

This directory contains opt-in evaluation workflows for Task Graph contributors.
They complement the deterministic unit suite:

```bash
python3 -m unittest discover
```

## Choose an evaluation

### Behavior cases

Behavior cases check whether Codex can turn a small plan and repository fixture
into valid Task Graph planning artifacts with the expected scheduling behavior.
The runner creates a repository from the case fixture, asks Codex to generate
the artifacts, validates them, and scores the result against the case contract.

Run one case from the repository root:

```bash
python3 -m evals.run_case --case evals/cases/001-disjoint
```

Behavior cases require Python and a working Codex CLI (`codex` by default). Use
`--codex-bin PATH` when the executable is available at a different path.

### Controller integration evals

Controller evals exercise the real controller lifecycle: parallel and serial
execution, retries, terminal failures, and controller recovery. They use a
scripted worker, but run the production controller through its CLI.

Run all controller scenarios with:

```bash
python3 -m evals.run_controller
```

Controller evals require Python, Git, and tmux. They do not require Codex.

## Running and debugging behavior cases

For a normal run, omit `--repo`. The runner materializes the fixture in a
temporary repository and removes it when the run finishes. It streams Codex
output, prints the generated DAG when available, and exits nonzero after
printing `FAIL:` messages if generation or scoring fails. A passing case prints
`PASS: <case-name>`.

To keep the materialized repository for inspection, supply a **new** destination
path:

```bash
python3 -m evals.run_case \
  --case evals/cases/002-shared-file \
  --repo /private/tmp/task-graph-eval-debug
```

`--repo` is for debugging only and must not already exist. The retained
repository includes `plan.md`, the fixture source, generated `.agent/` artifacts,
and `codex-output.txt`; inspect these after a failure. Because the runner
initializes and commits the fixture baseline, never point `--repo` at an
existing repository or a directory containing work you care about.

Controller evals report each scenario as `RUN`, `PASS`, or `FAIL`. On a failure,
the error includes a best-effort snapshot of the scenario, tmux session, and
controller state before cleanup. Check the command output first; unlike behavior
cases, controller scenarios do not accept `--repo` and always clean their
temporary resources.

Use the built-in interface help when you need the authoritative options:

```bash
python3 -m evals.run_case --help
python3 -m evals.run_controller --help
```

## Behavior-case anatomy

Each case lives under `evals/cases/<case-name>/`:

```text
<case-name>/
├── plan.md
├── repository/
├── setup.json
└── expected.json
```

- `plan.md` is the implementation plan given to Codex after the fixture is
  materialized.
- `repository/` is the committed baseline repository source. It should expose
  the scheduling surfaces the case is intended to test.
- `setup.json` describes setup performed after that baseline commit. Its
  `dirtyChanges` array can create intentional uncommitted edits for clean-base
  scheduling cases.
- `expected.json` is the scoring contract: the expected `planSlug`, task
  matching rules, dependencies, parallel-safety decisions, and required
  scheduling-rationale phrases.

The generated artifacts must be a valid `.agent/<plan-slug>/` directory with a
`dag.json` and the task briefs it references. The evaluator validates those
artifacts before comparing them with `expected.json`.

## Add a behavior case

1. Copy an existing directory in `evals/cases/` and give it a descriptive,
   ordered name.
2. Write a focused `plan.md` and the smallest `repository/` fixture that makes
   the intended overlap, disjointness, uncertainty, or dirty-worktree behavior
   observable.
3. Add `setup.json` (use an empty `dirtyChanges` array when no setup is needed)
   and define the expected scheduling contract in `expected.json`.
4. Run the case with `--repo PATH` while developing it, inspect the retained
   artifacts and transcript, then rerun without `--repo` to confirm ordinary
   cleanup behavior.
5. Add or update deterministic tests for evaluator or runner behavior as needed.

The evaluator is implemented in `evals/case_evaluator.py`. Its unit tests, plus
the behavior and controller runner tests, live in `evals/tests/`. The broader
Task Graph unit suite lives in `tests/` and is run with `python3 -m unittest
discover`.

## Cleanup and safety

Normal behavior-case runs use a temporary repository and remove it afterward.
Debug repositories supplied through `--repo` are intentionally retained for you
to remove after inspection. Controller scenarios create their repositories,
worktrees, and tmux sessions under temporary directories; they attempt to kill
their tmux session and remove the temporary directory whether a scenario passes
or fails, and report cleanup failures as evaluation failures. Run these evals
only when creating and removing those temporary Git and tmux resources is
acceptable on your machine.

