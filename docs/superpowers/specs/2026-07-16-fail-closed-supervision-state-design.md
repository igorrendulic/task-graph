# Fail-Closed Supervision-State Corruption Design

## Goal

Durable supervision corruption must not replay a completed wake or make queued
work disappear. Missing initial artifacts remain valid first-run state.

## Design

`kanban.py` will expose `SupervisionStateCorruption`, a typed exception that
includes the artifact path, an explanation, and an optional JSONL line number.
Strict supervision loaders will return an empty object only when the requested
artifact is absent. Malformed JSON and every non-object top-level JSON value
will raise that error.

Wake queue reads will validate each non-empty JSONL record before returning any
wakes. Required identity and action fields are non-empty strings: `id`,
`task`, `run_id`, and `action`. A malformed record raises the typed corruption
error identifying the queue path and source line.

The controller will perform all potentially corrupt supervision reads before it
resumes claimed wakes, supervises workers, claims wakes, acknowledges wakes, or
launches a dispatch. On corruption it persists a paused controller alert with
reason `SUPERVISION_STATE_CORRUPTION`, including the artifact path and line if
available. It then stops the drain without changing queue or claims state.

## Recovery

An operator must inspect and repair or replace the named state artifact. The
controller never repairs or discards it automatically. After repair, the
operator explicitly runs `controller.py start`; the existing persisted
controller dispatch record is then used to resume a claimed wake without
claiming it again.

## Verification

Regression tests cover malformed claims, a malformed queue line, absent initial
artifacts, and explicit restart after repair with a preserved claimed wake.
