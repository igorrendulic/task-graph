# Task Graph

> Turn an approved implementation plan into a safe, dependency-aware crew of coding workers.

## What it is

Task Graph is a Codex skill for breaking an approved plan into small, explicit task files, then running only the work whose dependencies are satisfied. It keeps scope, ownership, review evidence, and recovery state on disk instead of in a long-lived chat.

## Features

- Dependency-safe task batches and explicit task ownership.
- Dedicated Git worktrees and task branches for workers.
- Durable run records, reports, reviews, and portable diffs.
- Managed workers, unattended `codex exec`, or cloud delegation.
- Per-run delivery modes: `no-mistakes`, `direct-pr`, and `local-only`.
- Guarded delivery, teardown, and low-intrusion local-worker monitoring.

## Quick Start

### Requirements

Use Codex with Git. Unattended local workers also require tmux; PR delivery requires an authenticated GitHub CLI.

### Install for Codex

```bash
npx task-graph-skill@latest install --codex-only
```

### Create and run a task graph

```text
$task-graph tasks
$task-graph start
```

Task Graph derives a plan slug, writes task files under `.agent/<plan-slug>/`, and requires an explicit execution-mode selection before reserving work. There is no default mode: managed workers are in-session subagents, unattended `codex exec` uses non-interactive local CLI workers, and cloud delegation uses supported remote task execution. The controller records a run policy before workers launch, for example:

```bash
python3 <skill-dir>/scripts/kanban.py reserve --repo <repo-root> --plan <plan-slug> --limit 5 --run-id <run-id> --delivery-mode direct-pr
```

## How It Works

```text
Approved plan → task files → dependency-safe batch → dedicated worktrees
→ worker reports → review and verification → delivery policy → durable record
```

Task Graph keeps one integration branch for a plan. Each worker owns one task in a separate worktree; the controller owns board state, review, delivery, and recovery.

## Guarded Delivery

Guarded Delivery is the safe handoff from an isolated task worktree to delivery. It exists because a worker finishing its code is not the same as that code being ready to merge: the controller still needs to know what changed, confirm it was reviewed and tested, and avoid losing work during cleanup.

The flow is simple:

1. Choose how the completed work should be delivered.
2. Let the worker make the change in its own Git worktree.
3. Review and verify the result.
4. Deliver the verified change, then record what landed.
5. Record delivery before cleaning up that task's worktree and tmux session; do so after integration and verification, before marking it done.

### Choose a delivery mode

Every run chooses one mode before workers start:

- `no-mistakes`: choose this when the task must complete the project's full validation pipeline before Task Graph delivers a green PR.
- `direct-pr`: choose this when normal review and verification are sufficient, and Task Graph should deliver a PR without the extra pipeline.
- `local-only`: choose this when the work should stay local; Task Graph verifies and reviews it, then fast-forwards a clean local integration branch.

`+yolo` is optional. It lets the controller complete a routine green delivery after the required checks pass, so you do not need to approve that last ordinary step. It never skips failed verification or authorizes a security-sensitive action, an irreversible action, or an explicit discard.

### What Task Graph protects

Before `launch-exec`, Task Graph confirms that a worker is in its registered Git worktree on the right task branch, never in the controller checkout. It records the worktree, branch, and base commit so the controller can identify exactly what the worker started from. If it cannot recognize a running process, it marks it `UNKNOWN` and asks for inspection instead of guessing that it is safe to relaunch.

After a successful report, approved review, and tests, `delivery-ready` tells the controller which delivery action the chosen mode permits. `DONE` is review-ready only: retain the task worktree and tmux session through diff inspection, verification, task-commit creation, and integration. Once that task is integrated and verified, `record-delivery --result landed` records the result. Immediately run teardown before marking that task done; it removes both the dedicated worktree and its recorded tmux session. This per-task cleanup does not wait for the plan's final PR. Retain failed or retrying sessions for diagnosis; teardown still refuses dirty or unlanded work unless the controller explicitly chooses to discard it.

## Low-Intrusion Monitoring

The controller runs a bounded `watcher.py watch-exec --checkpoint --seconds 60` checkpoint immediately after launch, then repeats bounded checkpoints while work is expected. Checkpoint mode probes immediately, polls every five seconds, and returns early at `SUCCEEDED_AWAITING_REVIEW`, `NEEDS_ATTENTION`, `STALE`, or `UNKNOWN`. A quiet checkpoint prints its outcome and exits `124` after its bound.

Automatic monitoring must never use shell `sleep`, compound commands, manual `status --json` polling, or `status --watch`. Each checkpoint is a standalone read-only command; it never relaunches workers or changes task, runtime, report, delivery, or session state. Users may still request the status dashboards below.

## Controller reconciliation

Task Graph uses the board as the source of current work and treats old runtime records as history. Before reporting status or ending a controller turn, run `reconcile`; dispatch every safe review or first focused repair it returns. `No change` is valid only when the reconciliation queue has no autonomous work. While tasks are in flight, Codex controllers run bounded foreground `supervise` checkpoints; each checkpoint writes durable wakes under `.agent/<plan>/state/` before it signals the controller.

For FirstMate-style protection against a blind end, explicitly install the scoped Stop hook in a target project. It preserves existing hooks and blocks one turn only when reconciliation has actionable work:

```bash
python3 <skill-dir>/scripts/kanban.py install-stop-hook --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/kanban.py uninstall-stop-hook --repo <repo-root> --plan <plan-slug>
```

## Local controller

For unattended local workers, start one controller per plan. It requires tmux, records its lease, heartbeat, session, and every wake dispatch in `.agent/<plan-slug>/state/controller.json`. A second live controller for the same plan is refused; `start` is the explicit safe recovery action only after the prior tmux session is absent. It never auto-restarts a controller.

```bash
python3 <skill-dir>/scripts/controller.py start --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/controller.py start --repo <repo-root> --plan <plan-slug> --no-mistakes-command '<project gate command>'
python3 <skill-dir>/scripts/controller.py status --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/controller.py stop --repo <repo-root> --plan <plan-slug>
```

The controller claims durable wakes before it launches a reviewer or focused repair, then acknowledges them only after that session starts. Unexpected controller exceptions are appended to `state/controller-failures.jsonl` (the newest 50 are retained) and the latest record is exposed as `active_failure` in `controller.json`. Claimed wakes remain untouched, so after inspection an operator can explicitly run `controller.py start` to resume them through the normal protocol. The controller never auto-restarts or clears the active failure until a replacement tmux controller has launched successfully. Human-required wakes (`INSPECTION_REQUIRED`, `USER_CONTEXT_REQUIRED`, `RETRY_DECISION_REQUIRED`) and delivery approval or tooling failures are persisted as a pending alert, marked `escalated`, and pause the controller without discarding queued autonomous work. A `SUPERVISION_STATE_CORRUPTION` alert means the queue, claims, or dedupe state cannot be read safely: repair or replace the named artifact, then explicitly run `controller.py start`. The controller never auto-repairs that state or auto-restarts. Resolve other underlying task conditions, then use `start` to reconcile and resume. `status` reports `live`, `healthy`, pending alerts, active failures, and a recovery recommendation when the controller is dead or stale; it only reports, never restarts. `stop` is an explicit operator action; Stop hooks remain a backstop, not a dispatcher.

Delivery is policy-gated. With `+yolo`, `local-only` fast-forwards only a clean integration branch; `direct-pr` pushes through the PR/check/merge path; and `no-mistakes` first runs the command supplied at controller start, then follows the PR path. Without `+yolo`, the state records `DELIVERY_APPROVAL_REQUIRED` instead of landing work. External delivery commands are polled with named bounds while refreshing the controller heartbeat. A timeout before a mutating operation produces a precise delivery checkpoint. Before submitting a remote merge, the controller durably records the branch and expected task head; a merge timeout or interruption pauses at `DELIVERY_OUTCOME_UNKNOWN`. On the next `start`, it verifies the PR is merged at that expected head before finalizing it, and never retries an ambiguous merge automatically.

## Command Reference

Inspect the dependency graph:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --plan <plan-slug> --limit 5
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --plan <plan-slug> --limit 5 --json
```

Launch an already reserved unattended task:

```bash
python3 <skill-dir>/scripts/kanban.py launch-exec --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --branch task-graph/<plan-slug>/001-example --worktree <task-worktree>
tmux attach -t task-graph-<plan-slug>-<run-id>-001-example
```

Run the compact persistent monitor, or run an explicit bounded controller checkpoint. The watcher dashboard is read-only; controller supervision alone owns wake queue transitions. `status --watch` is a user-requested dashboard, not controller automation:

```bash
python3 <skill-dir>/scripts/watcher.py watch-exec --repo <repo-root> --seconds 180
python3 <skill-dir>/scripts/controller.py start --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/controller.py status --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/controller.py stop --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/kanban.py reconcile --repo <repo-root> --plan <plan-slug> --json
python3 <skill-dir>/scripts/kanban.py supervise --repo <repo-root> --plan <plan-slug> --seconds 60
python3 <skill-dir>/scripts/watcher.py watch-exec --checkpoint --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --seconds 60
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root>
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --json
python3 <skill-dir>/scripts/watcher.py status --repo <repo-root> --interval 2
```

Complete the post-worker lifecycle:

```bash
python3 <skill-dir>/scripts/kanban.py delivery-ready --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md
python3 <skill-dir>/scripts/kanban.py record-delivery --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --result landed
python3 <skill-dir>/scripts/kanban.py teardown --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md
python3 <skill-dir>/scripts/kanban.py done --repo <repo-root> --plan <plan-slug> --task 001-example.md
python3 <skill-dir>/scripts/kanban.py board --repo <repo-root> --plan <plan-slug>
```

Before integrating a completed `ship` task, create a portable diff package:

```bash
python3 <skill-dir>/scripts/kanban.py archive-diff --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --base <base-commit> --head <task-head-commit> --branch <task-branch> --review reviews/001-example.md
```

## Portable diff packages

Diff packages preserve the reviewed task delta and its metadata under `.agent/<plan-slug>/runs/<run-id>/diffs/`, so the controller can reconnect review evidence to the integrated change.

## Improvement Loop Checkpoints

When a `codex exec` worker reports `DONE_WITH_CONCERNS`, the controller reads the persisted report and begins one automatic focused repair-and-audit attempt. The retry uses a fresh isolated worker from the failed task branch's verified HEAD, inherits the selected execution and delivery policy, and receives a brief limited to the reported gap. The controller always reports the retry outcome to the user, whether the repair is ready for normal integration or remains unresolved.

The controller durably reserves a repair's child run, branch, and worktree before setup, but it consumes the automatic repair only after a valid child runtime record exists. A restart resumes that reservation instead of creating another worker. Failed setup artifacts remain available for inspection; if they conflict with the reservation, the controller raises `INSPECTION_REQUIRED` and never removes them automatically.

Only after that automatic retry still reports concerns does the controller stop and ask whether to stop with the current unresolved result or continue into another focused improvement-and-audit loop. Continue immediately launches exactly one linked repair-and-audit attempt; a later failed audit requires another Stop or Continue decision.

## Installation and Other Harnesses

Install for both Codex and Claude Code:

```bash
./install.sh
```

### Install for Claude Code

```bash
npx task-graph-skill@latest install --claude-only
```

For local development, use `./install.sh --link --force`.

## Task Contract

Each task has `Type`, `Goal`, `Context`, `Scope`, `Out Of Scope`, `Dependencies`, `Parallel`, `Acceptance Criteria`, and `Test Notes`. `Dependencies` is authoritative; `Parallel` is human guidance. `ship` tasks change code or docs; `scout` tasks produce a report and do not integrate code.

## Example Use Case

For a backend feature with schema, repository, API, tests, and docs, Task Graph creates separate task files only where work can be reviewed or executed independently. Dependents wait for their prerequisites; independent tasks run together.

## Contributing

Run the test suite with:

```bash
python3 -m unittest discover
```

## License

MIT.
