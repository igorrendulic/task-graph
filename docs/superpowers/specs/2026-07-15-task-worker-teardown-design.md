# Task Worker Teardown Design

## Goal

Make the point at which an unattended task worker is cleaned up explicit, and
ensure teardown removes its preserved tmux session as well as its worktree.

## Lifecycle

`DONE` means a worker is ready for review; it is not a teardown point. Keep the
tmux session and worktree while the controller inspects the report and diff,
runs verification, creates the task commit, and integrates that commit into the
plan integration branch.

After the task commit is integrated and its verification passes, the controller
records delivery with `record-delivery --result landed`. It then tears down that
task's dedicated worktree and recorded tmux session, before marking the task
done. This happens per task and does not wait for the plan's final PR to be
created or merged.

For failed or retrying work, retain the existing session and worktree for
diagnosis. For abandoned work, teardown remains permitted only after an
explicit discard.

## Changes

- Update the Task Graph skill and README with this concrete per-task ordering.
- Extend guarded `teardown` to kill the tmux session from the task's runtime
  record after its worktree is safely removed.
- Treat an already-absent tmux session as successful cleanup.
- Add tests for session removal and the already-absent case, while preserving
  existing landed/dirty/discard guards.

## Verification

Run the focused teardown tests, then the full repository test suite.
