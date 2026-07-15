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
- Helper: `scripts/kanban.py`

For every plan, read the supplied implementation plan, derive and announce a concise lowercase kebab-case `<plan-slug>` from its goal, then pass `--plan <plan-slug>` to every helper command. Reuse that slug when resuming the same plan. Run helper commands from any directory, passing `--repo <repo-root>` when the current directory is not the target repo. The target repo must contain `.agent/`; if it does not, ask before creating project workflow files. The helper never reads or updates the legacy shared `.agent/tasks`, `.agent/kanban.md`, or `.agent/runs` layout.

## Commands

### tasks command

Use this workflow when the user asks to create an implementation plan or task breakdown.

1. Inspect the requested feature and the relevant project code, docs, tests, and conventions.
2. Read the approved implementation plan, derive and announce its `<plan-slug>`, then decompose work into small implementation tasks that can be completed from a fresh context.
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
6. Keep each task scoped tightly enough for a fresh-context agent, and include project-specific commands or tests discovered from the repo in `Test Notes`; coalesce tightly coupled linear work when it shares one contract, code area, and acceptance cycle, and split only at independently reviewable milestones or true parallel boundaries.
7. Put new task files in `.agent/<plan-slug>/todo/`.
8. Regenerate `.agent/<plan-slug>/kanban.md` so TODO, IN PROGRESS, and DONE match the filesystem.

Use the helper after writing task files:

```bash
python3 <skill-dir>/scripts/kanban.py board --repo <repo-root> --plan <plan-slug>
```

## start command

Use this workflow when the user asks to start implementation.

1. Read `.agent/<plan-slug>/kanban.md` and the task files.
2. Choose a stable run id for this execution, for example `<plan-slug>-YYYYMMDD`, and inspect `.agent/<plan-slug>/runs/<run-id>/progress.md` if it already exists:
   - Treat tasks marked `complete` in the ledger as already done, even if conversational context was lost.
   - Reconcile any ledger/task-board mismatch before launching new work.
3. Run the helper `plan` command to determine which TODO tasks are startable in parallel and which must remain sequential or blocked:
   - A task is startable only when every dependency named in `Dependencies` is already in `done`.
   - Tasks can run in parallel when they are startable and neither task depends on the other.
   - Tasks with dependencies still in `todo` or `in-progress` are sequential or blocked and must not be launched.
   - Use a default parallel launch limit of `5` unless the user gives a different limit.
   - Use `plan --json` when machine-readable output is useful for spawning or bookkeeping.
4. Before reserving the batch, explicitly ask the operator which execution mode they want and explain every option:
   - `Managed workers`: in-session subagents, each in an isolated Git worktree and task branch.
   - **Unattended `codex exec`**: non-interactive local CLI workers, one per reserved task, running in tmux; the local machine or remote host must remain awake.
   - `Cloud delegation`: supported remote task execution; never silently fall back to local execution.
   The operator must explicitly choose one before continuing. There is no default mode. If no selection is supplied, the controller must not reserve tasks, create worktrees, write launch runtime records, or begin execution. An explicit mode in the start request counts as the selection. Record the selected mode in the run ledger before launching work.
5. Establish an integration branch for the overall implementation plan before launching work:
   - Use the current branch when it is already the intended feature branch.
   - Otherwise create or switch to a feature branch for the plan, for example `task-graph/<plan-slug>`.
   - The integration branch is the only branch that should be offered for a final GitHub PR.
6. Reserve the launch batch on the integration branch:
   - Require one delivery mode for the run: `no-mistakes`, `direct-pr`, or `local-only`. Record it with `reserve --plan <plan-slug> --run-id <run-id> --limit <n> --delivery-mode <mode>` before moving tasks to `in-progress`.
   - Add `--yolo` only when the operator explicitly authorizes green routine delivery for this run. +yolo never permits a red merge, a security-sensitive or irreversible action, or an explicit discard.
   - Keep task briefs in `.agent/<plan-slug>/runs/<run-id>/briefs/`, subagent reports in `.agent/<plan-slug>/runs/<run-id>/reports/`, review notes in `.agent/<plan-slug>/runs/<run-id>/reviews/`, and portable diff packages in `.agent/<plan-slug>/runs/<run-id>/diffs/`.
7. For every `Managed workers` or **Unattended `codex exec`** task, create a dedicated Git worktree and task branch from the integration branch:
   - Detect whether the controller is already in a linked worktree before creating more worktrees.
   - Prefer platform-native worktree/session tooling when it exists.
   - Use project-local `.worktrees/` or `worktrees/` only when that directory is ignored by git; otherwise use an external temp/worktree location.
   - Use a branch name that includes the task prefix and slug, for example `task-graph/<plan-slug>/001-add-schema`.
   - Never launch two worker agents in the same checkout.
   - Record the task branch, worktree path, base commit, and eventual head commit in the run ledger.
   - Never remove a worktree that contains unintegrated, unpushed, or otherwise unlanded work unless the user explicitly confirms discard.
   - Before `launch-exec`, verify that the selected worktree is a registered Git worktree root on the task branch and must not be the controller checkout.
8. For `Managed workers`, if more than one task is recommended in the launch batch, spawn worker agents for those tasks by default. Do not wait for the user to explicitly ask for parallel agents. Use this prompt shape for each worker:
   - You are working in a dedicated Git worktree on a dedicated task branch. Do not switch branches or edit another agent's worktree.
   - Own exactly one task file: `<task-file>`.
   - Read the task brief file first: `.agent/<plan-slug>/runs/<run-id>/briefs/<task-file>`.
   - Do not move `.agent/<plan-slug>/...` files and do not regenerate `.agent/<plan-slug>/kanban.md`; the main agent owns kanban state after integration.
   - Read only the task brief, this skill, done task artifacts if needed, and the minimum code required for the task.
   - Implement only the task's `Scope`; respect `Out Of Scope`.
   - Run the narrowest useful tests first, then broader tests when appropriate.
   - Commit the task's code, tests, and documentation changes on the task branch.
   - Write the full report to `.agent/<plan-slug>/runs/<run-id>/reports/<task-file>`.
   - Reply with only status, task branch, worktree path, commit SHA, one-line test summary, concerns, and report path.
9. For **Unattended `codex exec`**, tmux is required. After writing the task brief, use `launch-exec` for each reserved task in its dedicated worktree; it writes a durable runtime record before execution, captures output in a task-specific log, and preserves the exited pane for diagnosis. It runs `codex exec` with the normal workspace-write sandbox and the installed CLI's execution policy; never automatically use `--dangerously-bypass-approvals-and-sandbox`. The launcher persists the worker's final response to the controller-owned report path with `--output-last-message`; require that response to include one of the task status values below, a summary, tests, concerns, and a suggested commit message. The worker must edit and test only in its task worktree, and must not write run artifacts or commit. `DONE` means the task is ready for review, not cleanup: retain its worktree and tmux session through diff inspection, verification, task-commit creation, and integration. The runtime record contains the tmux session, pane PID (the process identifier), command, worktree, branch, brief/report/log paths, start/finish timestamps, and exit result. On resume, inspect the runtime record, report, and log before retrying; do not relaunch a completed task. Local `codex exec` still requires an awake machine or remote host.
10. Monitor each unattended local worker with low intrusion:
   - Run a standalone bounded checkpoint immediately after launch: `python3 <skill-dir>/scripts/kanban.py watch-exec --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task <task-file> --seconds 60`.
   - Repeat bounded `watch-exec` checkpoints while work is expected. Do not manually poll `status --json`; `watch-exec` probes immediately, then polls every five seconds and returns early for `SUCCEEDED_AWAITING_REVIEW`, `NEEDS_ATTENTION`, `STALE`, or `UNKNOWN`.
   - A quiet checkpoint exits `124` after its `--seconds` bound. It is read-only and must not reserve, relaunch, move, deliver, or clean up work.
   - The controller must never use shell `sleep`, compound commands, or `status --watch` for controller monitoring. `status` and `status --watch` remain explicit human dashboards.
11. For `Cloud delegation`, launch only when the selected Codex surface and workspace policy support it. Record the cloud task identifier, task branch/worktree or remote checkout reference, and result/report location in the run ledger. Do not fall back from cloud delegation to local execution without asking the operator.
12. Every worker, exec process, or cloud task must report one of these statuses:
   - `DONE`: implementation is complete and ready for review.
   - `DONE_WITH_CONCERNS`: implementation is complete, but the report lists correctness, scope, or maintainability concerns.
   - `NEEDS_CONTEXT`: the subagent needs specific missing information before continuing.
   - `BLOCKED`: the task cannot be completed as scoped.
13. Handle every result from the entire currently unblocked batch before launching its dependents:
   - For each `DONE`, ensure the task branch has a verified task commit. For unattended workers, the controller creates this commit from the dedicated worktree before continuing. Run `archive-diff` with the recorded task base and head commits, then run a task-scoped review for spec compliance and code quality. Link the patch and summary paths from the review note and final ledger entry.
   - For `DONE_WITH_CONCERNS`, `watch-exec` exposes the final report status at its wake-up boundary. The controller must read the persisted report and automatically launch exactly one focused repair-and-audit attempt before integration. It must not end the controller turn before launching that repair. Do not ask the user to nudge the controller or choose whether to run this first repair. The attempt inherits the execution mode, delivery mode, and `+yolo` setting; uses a fresh child worktree and branch from the failed task branch's verified HEAD; and receives a brief restricted to the reported concerns and failed evidence. Review and audit the repair through the normal task lifecycle.
   - After that repair attempt, always report the retry outcome to the user: repaired and ready for normal integration, still unresolved with the report path and remaining gap, or blocked/needs context. If it again returns `DONE_WITH_CONCERNS`, the controller must not automatically retry again; use the post-retry improvement checkpoint.
   - For `NEEDS_CONTEXT`, provide the missing context and re-dispatch the same task.
    - For `BLOCKED`, either provide context, use a more capable agent, split the task, or escalate to the user.
    - After a successful report, approved review, and test evidence, run `delivery-ready` to select the permitted controller action. `no-mistakes` requires its full pipeline, `direct-pr` opens a PR, and `local-only` fast-forwards only a clean integration branch. With +yolo, the controller may merge a green PR or fast-forward locally; otherwise it asks for approval. After the task commit is integrated and verification passes, record it with `record-delivery --result landed`, then immediately run teardown before marking that task done. Teardown removes both the dedicated worktree and recorded tmux session; it happens per task and does not wait for the plan's final PR. Retain failed or retrying sessions for diagnosis, and use `--discard` only after explicitly abandoning unlanded work.
14. If a task's `Type` is `scout`, capture its report in the run directory and mark it done after review; do not integrate code unless the user explicitly converts it into ship work.
15. If only one task is recommended, prefer the same worktree and task-branch flow unless the user explicitly asks for local in-checkout execution.
16. Before implementing a task locally, clear working context in practice:
   - Read only the selected task file, this skill, and the minimum code needed for that task.
   - Do not carry assumptions from previously completed tasks unless they are present in code, the selected task, or done task artifacts.
17. Implement only the selected task's `Scope`.
18. Run the narrowest useful tests first, then broader tests when appropriate.
19. Integrate completed and reviewed task branches back into the integration branch as one batch:
   - Merge or cherry-pick completed task branch commits in dependency order.
   - Resolve conflicts on the integration branch, not inside unrelated task worktrees.
   - Run the relevant verification once after the batch when its tasks are genuinely independent; only then mark each integrated task done and immediately calculate and reserve the next unblocked batch.
20. Move task files through `.agent/<plan-slug>/...` only from the integration branch:
   - Move the task to `done` only after its task branch is integrated and verification passes.
   - Regenerate `.agent/<plan-slug>/kanban.md` after task-state changes.
   - Append a `complete` entry to `.agent/<plan-slug>/runs/<run-id>/progress.md` with the relevant commits and review result.
21. After all ship tasks are integrated, run a final whole-branch review and the relevant verification.
22. Stop before creating any GitHub PR. Report the integration branch, commits, verification results, and review notes, then ask the user whether they want a PR created.
23. Create a GitHub PR only after the user explicitly confirms. Create separate PRs per task branch only when the user explicitly asks or the tasks are independently shippable.

### Post-retry improvement checkpoints

Use this checkpoint only after the automatic focused repair-and-audit attempt returns `DONE_WITH_CONCERNS` because the target outcome is still not met. An improvement loop is a focused implementation attempt followed by an audit or verification task for the same outcome.

Do not create, reserve, dispatch, or run another improvement loop until the user chooses what to do next. First read the audit report and present a concise checkpoint with:

- the report path and status;
- the target outcome or acceptance criterion;
- the measured result and remaining gap;
- the concerns or failed criteria listed in the report;
- the improvement loops already attempted in this run, when visible from `.agent/<plan-slug>/runs/<run-id>/reports/` or `progress.md`.

Ask the user to choose between:

- `Stop`: stop chasing the outcome for now, report the current unresolved state, and leave remaining work unresolved.
- `Continue`: authorize exactly one focused improvement-and-audit loop aimed only at the remaining gap.

Continue authorizes exactly one focused improvement-and-audit loop. After an explicit `Continue`, complete the following before ending the current controller turn:

1. Derive the child attempt id `<run-id>-task<task-prefix>-retry<N>`, where `N` is one greater than the visible prior attempts for that parent run and task. Create its run artifacts and append a `progress.md` entry naming the parent run and task, remaining gap, retry number, and inherited policy.
2. The child attempt inherits the parent execution mode, delivery mode, and `+yolo` setting. Write a focused retry brief containing only the remaining gap and the failed audit evidence.
3. Create a fresh child worktree and child branch from the failed task branch's verified HEAD; retain the failed worktree unless normal delivery or an explicit discard permits teardown.
4. Launch the inherited worker mode for the same in-progress task: dispatch the managed worker, run `launch-exec`, or launch the supported cloud task. The controller must not end the turn after only creating retry artifacts.
5. Review and audit that one repair attempt using the normal task lifecycle. If it still returns `DONE_WITH_CONCERNS`, return to this Stop/Continue checkpoint; do not create another attempt without a new explicit `Continue`.

Apply this checkpoint after each failed retry audit. Do not wait for repeated failures. If an audit returns `DONE` and the target outcome is met, continue the normal review, integration, verification flow, and required outcome update without asking for another loop.

Use the helper to plan parallel work without moving files:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --plan <plan-slug> --limit 5
```

Use JSON output for scripted launch bookkeeping:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --plan <plan-slug> --limit 5 --json
```

Reserve a launch batch and initialize the run ledger:

```bash
python3 <skill-dir>/scripts/kanban.py reserve --repo <repo-root> --plan <plan-slug> --limit 5 --run-id <run-id> --delivery-mode direct-pr
```

Use the helper to start the next task:

```bash
python3 <skill-dir>/scripts/kanban.py start --repo <repo-root> --plan <plan-slug>
```

Use the helper to finish a task after verification:

```bash
python3 <skill-dir>/scripts/kanban.py done --repo <repo-root> --plan <plan-slug> --task 001-example.md
```

Archive a completed task branch before integration:

```bash
python3 <skill-dir>/scripts/kanban.py archive-diff --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --base <base-commit> --head <task-head-commit> --branch <task-branch> --review reviews/001-example.md
```

Launch a prepared reserved task unattended (tmux is required):

```bash
python3 <skill-dir>/scripts/kanban.py launch-exec --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --branch task-graph/<plan-slug>/001-example --worktree <task-worktree>
```

Observe all active task executions without changing state:

```bash
python3 <skill-dir>/scripts/kanban.py watch-exec --repo <repo-root> --seconds 180
python3 <skill-dir>/scripts/kanban.py watch-exec --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --seconds 60
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root>
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --json
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root> --watch --interval 2
tmux attach -t task-graph-<plan-slug>-<run-id>-001-example
```

## Helper Behavior

The helper is intentionally conservative:

- `board --plan <plan-slug>` rewrites `.agent/<plan-slug>/kanban.md` from files present in its `todo`, `in-progress`, and `done` columns.
- `plan --plan <plan-slug> --limit <n>` prints the recommended parallel launch batch, additional startable tasks, and sequential or blocked tasks without moving files.
- `plan --json --limit <n>` prints the same scheduling decision as structured JSON.
- `reserve --plan <plan-slug> --limit <n> --run-id <id>` moves the recommended launch batch to `in-progress`, rewrites the board, and initializes `.agent/<plan-slug>/runs/<id>/progress.md`.
- `delivery-ready --plan <plan-slug> --run-id <id> --task <file>` refuses incomplete evidence and otherwise reports the policy-authorized controller delivery action.
- `record-delivery --plan <plan-slug> --run-id <id> --task <file> --result landed` records confirmed delivery so guarded teardown can distinguish landed work from discard.
- `start --plan <plan-slug>` selects the first startable todo task by filename, moves it to `in-progress`, rewrites the board, and prints the task path plus possible parallel candidates.
- `done --plan <plan-slug> --task <file>` moves a matching in-progress task to `done` and rewrites the board.
- `archive-diff --plan <plan-slug> --run-id <id> --task <file> --base <commit> --head <commit> --branch <branch> --review <relative-path>` validates an in-progress task and commit revisions, then writes a binary-capable patch and metadata summary to `.agent/<plan-slug>/runs/<id>/diffs/` without changing task state.
- `launch-exec --plan <plan-slug> --run-id <id> --task <file> --branch <branch> --worktree <path>` requires tmux and starts one reserved in-progress task in a deterministic session.
- `watch-exec [--plan <plan-slug>] [--run-id <id>] [--task <file>] --seconds <positive-int>` is a foreground-only, read-only controller checkpoint. It polls every five seconds, exits early on an actionable worker status, displays the persisted final report status, exits successfully when no active worker remains, and exits `124` on a quiet timeout.
- `status [--plan <plan-slug>] [--run-id <id>] [--task <file>] [--json]` is read-only. `status --watch [--interval <seconds>]` redraws the same data in place.
- Dependencies are parsed from the `## Dependencies` section as task filenames when present. `None` means no blocker.
- Task type is parsed from `## Type`; supported values are `ship` and `scout`, and omitted or unknown values default to `ship`.
- The `## Parallel` section is human guidance. Dependency parsing is authoritative for helper decisions.
- The run ledger is restart guidance. Tasks marked `complete` in `.agent/<plan-slug>/runs/<id>/progress.md` are not relaunched by `reserve`.

If the helper cannot confidently parse a dependency or choose a task, inspect the task files and update them before moving anything.
