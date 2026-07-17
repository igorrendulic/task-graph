# DAG generation

Use this reference only for the `tasks` workflow. It defines how to create task files, `kanban.md`, and the canonical `.agent/<plan-slug>/dag.json`.

## Workflow

1. Read the approved plan. Inspect the repository structure, relevant code, tests, docs, conventions, and uncommitted changes. Git history is out of scope.
2. Decompose the plan into small, fresh-context-ready tasks. Assign stable numeric filenames such as `001-add-schema.md`; use the filename stem as the stable DAG `id` (for example, `001-add-schema`).
3. Draft each task in `.agent/<plan-slug>/todo/` with these sections:
   - `Type` (`ship` or `scout`; default to `ship`)
   - `Goal`
   - `Context`
   - `Scope`
   - `Out Of Scope`
   - `Dependencies` (task filenames, or `None`)
   - `Parallel` (human-readable summary that agrees with the DAG)
   - `Predicted Paths and Symbols`
   - `Acceptance Criteria`
   - `Test Notes`
4. Predict every task's edited files and relevant symbols. Include contract surfaces, tests, generated artifacts, and docs when they may be touched. If the surface cannot be established confidently, record it as uncertain.
5. Build the DAG from the same task drafts. `dependsOn` is the sole scheduling authority; do not create a duplicate conflict matrix or use `parallelSafe` to override dependencies.
6. Schedule conservatively:
   - A task is `parallelSafe: true` only when its predicted edit surface is demonstrably disjoint from every task that could otherwise run beside it.
   - Serialize tasks that share a file, symbol, public contract, test, generated artifact, or any uncertain surface.
   - When overlapping tasks have no natural prerequisite, preserve source-plan order: the later task depends on the earlier task.
   - If dirty local changes overlap a task's predicted surface, still generate the DAG, but set `parallelSafe: false` and explain that a clean base is required before it can be safely executed. Serialize it against potentially overlapping work in source-plan order.
7. Keep task-file `Dependencies` and DAG `dependsOn` consistent using the task ID-to-filename mapping. `dag.json.dependsOn` contains prerequisite task IDs, while each task brief's `## Dependencies` section contains the corresponding prerequisite `taskFile` filenames. For example, if task `002-add-serializer` depends on task `001-add-config-parser`, write `"dependsOn": ["001-add-config-parser"]` in `dag.json` and `- 001-add-config-parser.md` in `002-add-serializer.md`.
8. Before replacing the canonical DAG or reporting success, validate the staged graph: every task ID is unique, every task filename is unique, each dependency names a known task, no task depends on itself, and the graph is acyclic. Use `python3 <skill-dir>/scripts/dag_validation.py --dag <staged-dag.json> --plan-dir <plan-dir>` when the validator is available. Invalid references or cycles must fail the run without writing a successful `dag.json`.
9. Write or refresh all task files first, then regenerate `kanban.md` from the task folders and generate or replace `.agent/<plan-slug>/dag.json` in the same `tasks` run. On rerun, refresh the DAG from the current approved plan and repository state rather than merging stale scheduling data.
10. Report the plan slug, task files, DAG path, independent roots, serialized tasks, and any dirty-worktree caveats.

## DAG format (v1)

`dag.json` is the canonical scheduling artifact for the plan. Its shape is:

```json
{
  "schemaVersion": 1,
  "planSlug": "example-plan",
  "tasks": [
    {
      "id": "001-add-schema",
      "taskFile": "001-add-schema.md",
      "title": "Add the schema",
      "instructions": "Implement the task file's Scope and Acceptance Criteria.",
      "predictedPaths": ["src/schema.py", "tests/test_schema.py"],
      "predictedSymbols": ["Schema", "parse_schema"],
      "dependsOn": [],
      "parallelSafe": true,
      "schedulingRationale": "Its predicted code and test surfaces are disjoint from the other root task."
    }
  ]
}
```

Rules:

- `schemaVersion` is the number `1`; `planSlug` exactly matches the containing directory.
- `id` and `taskFile` are stable and unique. `taskFile` is a basename in that plan's task folders.
- `instructions` is enough direction to execute the task from a fresh context and agrees with its task file.
- `predictedPaths` and `predictedSymbols` describe the evidence used to schedule; use an explicit uncertain entry when necessary instead of silently omitting unknown overlap.
- `dependsOn` contains only task IDs. A task may begin only after every listed ID is complete.
- Each task brief's `## Dependencies` section contains the matching `taskFile` filenames, not bare task IDs. Map every `dependsOn` ID through that task's `taskFile` value before writing the brief. For example:

  ```json
  "dependsOn": ["001-add-config-parser"]
  ```

  ```markdown
  - 001-add-config-parser.md
  ```
- `parallelSafe` describes whether the task has a demonstrably disjoint edit surface. It never authorizes execution that violates `dependsOn`.
- `schedulingRationale` explains the dependency and parallel-safety decision, including shared surfaces, uncertainty, preserved source order, or dirty-worktree requirements.

## v1 boundary

Version 1 only plans work. The `tasks` workflow must not create feature branches, task branches, worktrees, worker sessions, merges, or pull requests. A later execution workflow may consume `dependsOn` to start and integrate work in dependency order.
