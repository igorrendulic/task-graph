# Task Graph README redesign

## Goal

Make `README.md` a clear, Codex-first entry point that explains what Task Graph does, how to install it, and how to use it without forcing a new reader through controller internals.

## Audience

Primary audience: Codex users who have an approved implementation plan and want a durable, dependency-aware way to run implementation tasks. Claude Code users remain supported through a brief alternative installation section.

## Information architecture

1. Open with Task Graph's purpose and the situations where it is useful.
2. Put requirements, the Codex install command, and the first-use workflow before every operational detail.
3. Show the workflow as a compact diagram: approved plan → task files → dependency-safe work → review and delivery record.
4. Include a focused tmux monitoring section with the attach command emitted by the controller. Add one real screenshot only if its captured contents are safe to publish and legible.
5. Explain the few concepts needed to operate the skill: tasks and dependencies, isolated worktrees, execution modes, and delivery modes.
6. Retain a compact command reference and alternatives for Claude Code and local development.
7. Omit detailed recovery, queue, and controller implementation narratives from the primary README; `SKILL.md` remains the operational contract.

## Screenshot

The screenshot will be captured from an existing Task Graph tmux session, never fabricated. It will show only non-sensitive status/output. If no suitable session is available, the README will use a text command example instead of an image.

## Validation

- Check the README's commands against the installer and controller scripts.
- Run the documentation-focused unit tests.
- Review the rendered Markdown structure and final diff for readability.
