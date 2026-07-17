# Merge worktree cleanup

## Purpose

Make `checkout` non-destructive and move optional integration-worktree cleanup
to the end of a successful `merge` operation.

## Command behavior

- `checkout <plan-slug> --run-id <run-id>` only validates the run and primary
  checkout, then switches the primary checkout to the run's feature branch. It
  does not inspect, remove, or otherwise alter the integration worktree.
- After `merge <plan-slug> --run-id <run-id>` successfully creates its merge
  commit, it checks whether that run's integration worktree exists.
- If the integration worktree exists and is clean, `merge` prompts on the
  terminal: `Remove the clean integration worktree? [y/N]`.
- Only an explicit `y` removes the worktree with the existing non-force removal
  method. Any other response leaves it in place.
- If the integration worktree is dirty, cleanup is skipped and the command
  reports why. It never force-removes a worktree.
- No cleanup prompt is shown before the merge. If the merge conflicts, the
  existing safe abort behavior remains unchanged.

## Documentation and tests

- README workflow language describes `checkout` as branch switching only and
  describes the post-success merge cleanup prompt.
- CLI tests prove checkout never invokes worktree removal, successful merge
  offers and honors cleanup, and a dirty worktree is retained.
