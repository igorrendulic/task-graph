# Guarded Worker Lifecycle Design

## Goal

Make Task Graph’s unattended workers as safe and recoverable as its task graph:
enforce isolated worktrees, record per-run delivery policy, distinguish live
workers from idle panes, and deliver verified work automatically only when the
operator explicitly selects `+yolo` for that run.

## Run policy

`reserve` will require one delivery mode for every new run and persist it in
the run ledger before any worker launches:

- `no-mistakes`: the worker completes the project’s no-mistakes validation
  pipeline, then creates and delivers a green PR.
- `direct-pr`: Task Graph verifies and reviews the task, then pushes, opens,
  and delivers a PR without requiring the no-mistakes pipeline.
- `local-only`: Task Graph verifies and reviews the task, then integrates it
  locally with a clean fast-forward.

`+yolo` is a per-run modifier. It authorizes the controller to perform the
normal merge or local fast-forward after all required verification and review
are green. It never authorizes a red merge, a security-sensitive or
irreversible action, or discarding work.

Without `+yolo`, Task Graph stops after reporting a verified PR or verified
local branch and asks the operator to authorize delivery.

## Worktree and runtime contract

Before `launch-exec` creates a tmux session, the helper must verify that the
given path is a Git worktree root, differs from the controller repository
checkout, and has the requested task branch checked out. It records the
verified worktree path, branch, and base commit in the runtime record. Any
failed boundary check aborts before runtime state or tmux state is created.

Runtime state remains controller-owned. Workers may change only their assigned
worktree and return a report; they do not commit or change task-board state.

## Worker liveness and monitoring

Status inspection classifies tmux workers as `RUNNING`, `IDLE_OR_DEAD`, or
`UNKNOWN` based on process-aware evidence rather than session existence alone.
`UNKNOWN` is conservative: it provides a diagnostic action and never triggers
an automatic relaunch. Existing automatic monitoring remains one standalone
JSON status probe immediately after launch, followed by platform-native
60-second waits before subsequent probes; it stops on terminal status.

## Delivery and cleanup

After a worker reports `DONE`, the controller verifies its diff and tests,
creates the task-branch commit, archives its diff, and completes task review.
The chosen delivery mode then governs publication or integration. A delivery
operation is permitted only after all required checks are green; `+yolo`
removes only the routine operator confirmation.

Teardown refuses a worktree containing uncommitted or unlanded work. The sole
override is an explicit `discard` confirmation. Successful teardown records
the delivery result in the run ledger before removing any owned worktree.

## Verification

Automated coverage will prove:

- invalid, primary-checkout, and branch-mismatched worktrees are rejected;
- runtime records retain verified base and worktree identity;
- live, idle/dead, and unknown tmux states classify safely;
- each delivery mode follows its intended handoff and `+yolo` never bypasses
  failed verification;
- teardown refuses dirty or unlanded work and accepts only explicit discard;
- the skill and README describe the same policy and monitoring cadence.
