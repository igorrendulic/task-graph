# Task Graph

> Turn an approved implementation plan into small, dependency-aware coding tasks that can be run, reviewed, and recovered safely.

## What it is

Task Graph is a Codex skill for turning an approved plan into explicit task files, then starting only the work whose dependencies are complete. It keeps task scope, ownership, review evidence, and recovery state in the repository rather than in a long-lived chat.

Use it when a change is large enough to benefit from isolated worktrees, parallel workers, or a durable record of how work was reviewed and delivered.

## Features

- Dependency-safe batches with one task owner at a time.
- Separate Git worktrees and branches for worker changes.
- Durable run records, reviews, and portable diff packages.
- Managed workers, unattended `codex exec`, or cloud delegation.
- Guarded delivery with `no-mistakes`, `direct-pr`, and `local-only` modes.

## Quick Start

### Requirements

Use Codex with Git. Unattended local workers require tmux; PR delivery also requires an authenticated GitHub CLI.

### Install for Codex

```bash
npx task-graph-skill@latest install --codex-only
```

### Turn a plan into tasks, then start work

In Codex, after your implementation plan is approved:

```text
$task-graph tasks
$task-graph start
```

Task Graph derives a plan slug and writes the board and task files below `.agent/<plan-slug>/`. Before any work is reserved, it requires an explicit execution-mode selection. There is no default mode:

- Managed workers run as in-session subagents.
- Unattended `codex exec` workers are non-interactive local CLI workers.
- Cloud delegation uses supported remote task execution.

The run also chooses a delivery mode. For example, the controller records `direct-pr` before workers launch:

```bash
python3 <skill-dir>/scripts/kanban.py reserve \
  --repo <repo-root> --plan <plan-slug> --limit 5 \
  --run-id <run-id> --delivery-mode direct-pr
```

## How It Works

```text
Approved plan → task files → dependency-safe batch → isolated worktrees
→ review and verification → delivery policy → durable record
```

One integration branch represents the plan. Each worker changes one task in its own worktree; the controller owns the board, review, delivery, and recovery.

### What happens after `$task-graph start`

1. Task Graph chooses the next unblocked task or batch.
2. Workers receive focused task files and report their outcome.
3. The controller reviews and verifies completed changes.
4. Verified work is delivered according to the run policy and recorded before cleanup.

## Monitor a Run

For unattended local work, start the repository controller. It prints the exact session name to attach to:

```bash
python3 <skill-dir>/scripts/controller.py start \
  --repo <repo-root> --plan <plan-slug>

# Output: Connect: tmux attach -t task-graph-controller-<plan-slug>
tmux attach -t task-graph-controller-<plan-slug>
```

Use the watcher for a live, read-only dashboard immediately after launch. It polls every five seconds and checkpoint mode returns early at `SUCCEEDED_AWAITING_REVIEW`, `NEEDS_ATTENTION`, `STALE`, or `UNKNOWN`. Separately, the controller runs bounded `supervise` checkpoints to persist and dispatch its own recovery work.

```bash
python3 <skill-dir>/scripts/watcher.py watch-exec \
  --checkpoint --repo <repo-root> --plan <plan-slug> \
  --run-id <run-id> --task 001-example.md --seconds 60

python3 <skill-dir>/scripts/watcher.py status \
  --repo <repo-root> --interval 2
```

Automatic monitoring must never use shell `sleep`, compound commands, manual `status --json` polling, or `status --watch`. Each checkpoint is standalone and read-only: it does not relaunch workers or mutate task, runtime, report, delivery, or session state.

## Core Concepts

### Task files and dependencies

Task files declare their goal, scope, dependencies, parallelism, acceptance criteria, and test notes. Dependencies are authoritative; Task Graph will not start a task until its prerequisites are done. The board lives at `.agent/<plan-slug>/kanban.md`.

### Isolated worktrees

Workers never edit the controller checkout. Before `launch-exec`, Task Graph records the worker worktree, branch, and base commit so it can verify that the task is running in its registered location.

## Guarded Delivery

Guarded Delivery is the safe handoff from an isolated task worktree to delivery. A worker finishing code is not the same as that code being ready to merge: the controller still reviews it, runs the required verification, and preserves the delivery record.

1. Choose how the completed work should be delivered.
2. Let the worker make the change in its own Git worktree.
3. Review and verify the result.
4. Deliver the verified change, then record what landed.
5. Record delivery before cleaning up the task worktree and tmux window.

Choose one run-wide mode:

- `no-mistakes` runs the project's full validation pipeline before Task Graph delivers a green PR.
- `direct-pr` uses normal review and verification before opening a PR.
- `local-only` keeps delivery local and fast-forwards only a clean integration branch.

`+yolo` permits routine green delivery after required checks pass. It never authorizes a security-sensitive action, an irreversible action, or an explicit discard.

## Command Reference

Inspect startable work:

```bash
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --plan <plan-slug> --limit 5
python3 <skill-dir>/scripts/kanban.py plan --repo <repo-root> --plan <plan-slug> --limit 5 --json
```

Launch an already reserved unattended task and attach to its plan session:

```bash
python3 <skill-dir>/scripts/kanban.py launch-exec \
  --repo <repo-root> --plan <plan-slug> --run-id <run-id> \
  --task 001-example.md --branch task-graph/<plan-slug>/001-example \
  --worktree <task-worktree>
tmux attach -t task-graph-<plan-slug>
```

Observe or manage the controller:

```bash
python3 <skill-dir>/scripts/controller.py start --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/controller.py status --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/controller.py stop --repo <repo-root> --plan <plan-slug>
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root>
python3 <skill-dir>/scripts/kanban.py status --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --json
```

Complete the post-worker lifecycle:

```bash
python3 <skill-dir>/scripts/kanban.py delivery-ready --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md
python3 <skill-dir>/scripts/kanban.py record-delivery --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md --result landed
python3 <skill-dir>/scripts/kanban.py teardown --repo <repo-root> --plan <plan-slug> --run-id <run-id> --task 001-example.md
python3 <skill-dir>/scripts/kanban.py done --repo <repo-root> --plan <plan-slug> --task 001-example.md
```

Before integrating a completed `ship` task, preserve its reviewable delta:

```bash
python3 <skill-dir>/scripts/kanban.py archive-diff \
  --repo <repo-root> --plan <plan-slug> --run-id <run-id> \
  --task 001-example.md --base <base-commit> --head <task-head-commit> \
  --branch <task-branch> --review reviews/001-example.md
```

## Controller Safety Notes

The controller keeps the current board authoritative and treats old runtime records as history. Before reporting status or ending a controller turn, run `reconcile`; `No change` is valid only when there is no autonomous action. While work is in flight, use bounded `supervise` checkpoints.

The tmux-resident controller records its plan state at `.agent/<plan-slug>/state/controller.json`. It never auto-restarts. Unexpected exceptions are written to `controller-failures.jsonl`, with the newest failure exposed as `active_failure`; Claimed wakes remain untouched so an operator can inspect the condition and explicitly run `controller.py start` to resume safely. A `SUPERVISION_STATE_CORRUPTION` alert means the controller cannot safely read its queue or claims: repair or replace the named artifact, then explicitly start the controller again.

## Portable diff packages

Portable diff packages live under `.agent/<plan-slug>/runs/<run-id>/diffs/`. They preserve a reviewed task delta and metadata so the controller can reconnect review evidence to the integrated change.

## Improvement Loop Checkpoints

When a `codex exec` worker reports `DONE_WITH_CONCERNS`, the controller performs one automatic focused repair-and-audit attempt and always reports the retry outcome. Only after that automatic retry still reports concerns does it ask whether to stop with the current unresolved result or continue into another focused improvement-and-audit loop. Continue immediately launches exactly one linked repair-and-audit attempt; a later failed audit requires another Stop or Continue decision.

## Other Installation Options

Install for both Codex and Claude Code:

```bash
./install.sh
```

### Install for Claude Code

```bash
npx task-graph-skill@latest install --claude-only
```

For local development, use `./install.sh --link --force`.

## Contributing

Run the full test suite with:

```bash
python3 -m unittest discover
```

## License

MIT.
