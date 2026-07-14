# Mandatory Execution Mode Selection

## Goal

Make every Task Graph execution start explicitly require the operator to select an execution mode before any task is reserved or launched.

## Behavior

On every `$task-graph start`, the controller must present these choices and their meanings:

- **Managed workers**: in-session subagents, each working in an isolated Git worktree and task branch.
- **Unattended `codex exec`**: non-interactive local CLI workers, one per reserved task, running in tmux. The local machine or remote host must remain awake.
- **Cloud delegation**: supported remote task execution. The controller must not silently fall back to local execution.

The controller must then wait for an explicit operator selection. There is no default mode. If no selection is supplied, it must not reserve tasks, create worktrees, write launch runtime records, or begin any worker or cloud task.

An explicitly named mode in the operator's start request counts as that selection. Once selected, the controller records the mode in the run ledger before launching the reserved batch.

## Documentation and Verification

`SKILL.md` is the operational contract and will state the mandatory prompt, the per-start mode explanations, and the no-default gate. `README.md` will describe the same behavior for users. Documentation tests will assert the mandatory-choice wording and reject the old default behavior.

No changes are needed to the helper CLI because execution-mode selection is an agent-controller interaction, not a helper command.

## Error Handling

If the selected mode is unavailable, the controller reports the specific prerequisite or policy limitation and waits for the operator to choose another mode or provide the needed capability. It does not substitute another mode automatically.

## Scope

This change affects Task Graph execution instructions, user-facing documentation, and their contract tests only. It does not alter task scheduling, task-file state transitions, worktree isolation, or the `launch-exec` runtime implementation.
