# Controller Failure Journal and Recovery Context

## Goal

When the tmux-resident controller terminates because of an unexpected exception,
leave actionable, durable diagnostic evidence for the operator. The controller
must not restart itself or change the durable wake-claim protocol.

## Storage

Each plan stores a `controller-failures.jsonl` journal alongside
`state/controller.json`. A write retains the latest 50 complete JSON records in
chronological order and replaces the journal atomically. This deterministic cap
prevents unbounded state growth while retaining recent history.

`controller.json` gains an `active_failure` field. It is either `null` or the
most recently recorded journal record. Journal history is never cleared by a
restart.

Each failure record contains:

- an ISO-8601 timestamp;
- the controller phase (`heartbeat`, `drain`, or `supervise`);
- exception class and message;
- the most recent dispatch/wake identifier when known; and
- a bounded traceback summary.

## Controller behavior

The controller loop places an exception boundary around each operational phase.
On an unexpected exception, it writes the journal record and active marker,
then exits the controller process without retrying or restarting. Known
fail-closed supervision corruption remains a normal paused-alert path rather
than an unexpected controller failure.

The boundary does not acknowledge, escalate, dequeue, or otherwise mutate a
claimed wake. A subsequent explicit `controller.py start` uses the existing
`resume_claimed_wakes` protocol to resume it.

## Recovery behavior

`controller.py status` reports `active_failure`. If the controller session is
dead or its heartbeat is stale, status provides a recovery recommendation to
inspect the failure and explicitly run `controller.py start`; it remains
read-only and never restarts the controller.

An explicit start preserves `active_failure` until `tmux_start` succeeds. Only
after that successful launch does it clear the active marker. A failed launch
therefore leaves diagnostic context intact.

## Verification

Regression tests cover failures during supervision and review dispatch,
including preservation of claimed wakes and capture of their wake IDs. Tests
also cover the start timing of active-marker clearing, status recovery context,
and deterministic retention of the newest 50 journal entries.
