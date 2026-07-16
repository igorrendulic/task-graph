# Transactional Repair Dispatch

## Purpose

Ensure a repair setup failure cannot consume a task's one automatic repair attempt. A repair becomes counted only after the child worker has a valid persisted runtime record.

## State model

Task supervision state retains compatibility with the legacy `repair_attempts` integer and adds a `repair_attempt` object:

```json
{
  "task": "001-work.md",
  "repair_attempts": 0,
  "repair_attempt": {
    "attempt": 1,
    "child_run_id": "run-a-task001-repair1",
    "branch": "task-branch-repair-1",
    "worktree": "/tmp/task-graph-plan-run-a-task001-repair1-001-work",
    "phase": "reserved"
  }
}
```

`repair_attempts` remains readable for old state files. It represents launched attempts for new writes. The record phase is one of:

- `reserved`: identity is durably allocated, but no valid child runtime is known.
- `launched`: the child runtime record exists and is valid; this is the sole phase that consumes the automatic repair.
- `failed`: setup failed before launch; it does not consume the automatic repair and its artifacts remain untouched.

## Dispatch behavior

1. The controller reads any existing attempt record before allocating identity.
2. With no active reservation, it creates and atomically writes a `reserved` record before writing briefs or creating a worktree.
3. It reuses a reserved record's child run ID, branch, and worktree after a restart instead of allocating a new one.
4. Before launching, it checks for a child runtime record. A valid record transitions the reservation to `launched` and returns its session without launching again.
5. If setup completes and launch yields a valid runtime record, the controller transitions the record to `launched`.
6. A worktree/setup failure transitions the record to `failed`, preserves every existing artifact, and returns an eligible repair outcome.
7. If preserved artifacts conflict with the reservation (for example a manually repaired or inconsistent worktree), dispatch emits `INSPECTION_REQUIRED`; it never removes them automatically.

## Reconciliation behavior

Repair-limit decisions count only launched records. A `reserved` record is reconciled by the controller as the same attempted setup, never by relaunching blindly. A `failed` record leaves the task eligible for `REPAIR_REQUIRED`; a launched record results in `RETRY_DECISION_REQUIRED` after another concern or rejected review.

## Tests

- A worktree creation failure leaves `repair_attempts` at zero and reconciliation returns `REPAIR_REQUIRED`.
- A restart after reservation uses the existing child run ID, branch, and worktree.
- A restart after launch finds the valid runtime record, marks/retains the attempt as launched, and does not start another worker.
- A completed launched repair still produces `RETRY_DECISION_REQUIRED`.

## Non-goals

The controller does not delete, overwrite, or repair inconsistent reserved artifacts. Those conditions explicitly require operator inspection.
