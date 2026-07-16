# Watcher Observation Failure Design

## Goal

Ensure the read-only watcher never treats an inability to collect runtime status as a quiet or completed system.

## Behavior

Each `kanban.collect_status()` call is an observation boundary. A filesystem or status-decoding exception has mode-specific handling.

- `watch-exec --checkpoint` prints `observation error: <message>` and returns exit code `2` immediately. It does not sleep, print a no-worker checkpoint, or return the normal timeout code.
- Interactive `watch-exec` renders `Observation error: <message>` in the dashboard area, waits for the normal watcher interval, and retries. A later successful collection restores the normal dashboard.
- `status` follows the dashboard rule, retrying at its configured interval. JSON mode returns an error rather than pretending the task list is empty.

## Constraints and Tests

The watcher stays read-only: no wake creation, controller mutation, or worker restart. Tests prove checkpoint failure exits `2` without sleeping and dashboard failure remains visible before a successful retry; existing no-worker and actionable checkpoint tests remain unchanged.
