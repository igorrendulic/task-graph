# Codex-First README Design

## Goal

Restructure Task Graph’s README into a guided, Codex-first product document
that explains the worker lifecycle before presenting the complete command
reference.

## Audience and ordering

The primary reader uses Codex to turn an approved implementation plan into
safe parallel work. Claude Code installation remains supported, but appears
after the Codex quick start rather than competing with it at the top.

The README will use this order:

1. A one-sentence promise and a short "What it is" explanation.
2. A feature list covering task graphs, isolated worktrees, durable runs,
   execution modes, guarded delivery, and low-intrusion monitoring.
3. A Codex-first quick start with prerequisites, installation, task creation,
   explicit execution-mode choice, and a run-policy example.
4. A compact "How it works" text flow from approved plan through task graph,
   dedicated worktrees, review, delivery, and durable records.
5. Delivery and safety reference: `no-mistakes`, `direct-pr`, `local-only`,
   per-run `+yolo`, controller-checkout refusal, conservative liveness,
   `delivery-ready`, `record-delivery`, and explicit-discard teardown.
6. Monitoring reference: immediate standalone JSON probe, platform-native
   60-second waits, and terminal stop states.
7. Full helper command reference, installation variants, task contract,
   example use case, contributing, and license.

## Content rules

- Reuse Task Graph terminology; do not import FirstMate’s captain, crew,
  secondmate, or watcher-daemon concepts.
- Preserve all existing helper commands and their plan-scoped paths.
- State that `+yolo` can automate only routine green delivery; it cannot
  authorize failed verification, security-sensitive actions, irreversible
  actions, or discard.
- Keep user-requested `status --watch` examples separate from automatic
  controller monitoring, which must never use it.
- Keep the README self-contained; it is the public explanation of the skill,
  while `SKILL.md` remains the complete controller runbook.

## Verification

Documentation-contract tests will retain the plan scope, mode selection,
tmux launcher, monitoring, and guarded delivery assertions. The rendered
README must have a single H1, a Codex quick-start path before the Claude Code
variants, and an explicit delivery lifecycle before the command reference.
