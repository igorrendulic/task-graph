import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillDocsTest(unittest.TestCase):
    def test_docs_describe_supervision_corruption_recovery(self) -> None:
        docs = (ROOT / "SKILL.md").read_text(encoding="utf-8") + (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("SUPERVISION_STATE_CORRUPTION", docs)
        self.assertIn("repair or replace", docs)
        self.assertIn("explicit", docs)

    def test_readme_documents_controller_failure_journal_recovery(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("controller-failures.jsonl", readme)
        self.assertIn("active_failure", readme)
        self.assertIn("Claimed wakes remain untouched", readme)
        self.assertIn("never auto-restarts", readme)

    def test_docs_require_a_plan_scoped_board(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(".agent/<plan-slug>/kanban.md", skill)
        self.assertIn("--plan <plan-slug>", skill)
        self.assertIn("never reads or updates the legacy shared", skill)
        self.assertIn(".agent/<plan-slug>/", readme)
        self.assertIn("--plan <plan-slug>", readme)

    def test_post_retry_failed_audit_requires_user_checkpoint_before_another_loop(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Post-retry improvement checkpoints", skill)
        self.assertIn("automatic focused repair-and-audit attempt", skill)
        self.assertIn("Do not create, reserve, dispatch, or run another improvement loop", skill)
        self.assertIn("`Stop`", skill)
        self.assertIn("`Continue`", skill)

    def test_continue_launches_one_linked_improvement_attempt(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Continue authorizes exactly one focused improvement-and-audit loop", skill)
        self.assertIn("must not end the turn after only creating retry artifacts", skill)
        self.assertIn("<run-id>-task<task-prefix>-retry<N>", skill)
        self.assertIn("inherits the parent execution mode, delivery mode, and `+yolo` setting", skill)
        self.assertIn("fresh child worktree and child branch from the failed task branch's verified HEAD", skill)
        self.assertIn("return to this Stop/Continue checkpoint", skill)
        self.assertIn("Continue immediately launches exactly one linked repair-and-audit attempt", readme)
        self.assertIn("a later failed audit requires another Stop or Continue decision", readme)

    def test_readme_documents_improvement_loop_checkpoint(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Improvement Loop Checkpoints", readme)
        self.assertIn("Only after that automatic retry", readme)
        self.assertIn("stop with the current unresolved result", readme)
        self.assertIn("continue into another focused improvement-and-audit loop", readme)

    def test_done_with_concerns_requires_one_automatic_retry_and_outcome_update(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("automatically launch exactly one focused repair-and-audit attempt", skill)
        self.assertIn("must not end the controller turn", skill)
        self.assertIn("always report the retry outcome to the user", skill)
        self.assertIn("must not automatically retry again", skill)
        self.assertIn("automatic focused repair-and-audit attempt", readme)
        self.assertIn("always reports the retry outcome", readme)

    def test_docs_require_batch_execution_and_diff_packages(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("coalesce tightly coupled linear work", skill)
        self.assertIn("entire currently unblocked batch", skill)
        self.assertIn("archive-diff", skill)
        self.assertIn("runs/<run-id>/diffs/", skill)
        self.assertIn("archive-diff", readme)
        self.assertIn("Portable diff packages", readme)

    def test_start_requires_an_execution_mode_with_unattended_exec_contract(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("explicitly choose one before continuing", skill)
        self.assertIn("must not reserve tasks, create worktrees, write launch runtime records, or begin execution", skill)
        self.assertIn("`Managed workers`: in-session subagents", skill)
        self.assertIn("non-interactive local CLI workers", skill)
        self.assertIn("`Cloud delegation`: supported remote task execution", skill)
        self.assertNotIn("Managed workers (default)", skill)
        self.assertIn("requires an explicit execution-mode selection", readme)
        self.assertIn("no default mode", readme)
        self.assertIn("in-session subagents", readme)
        self.assertIn("non-interactive local CLI workers", readme)
        self.assertIn("supported remote task execution", readme)
        self.assertNotIn("Managed workers (default)", readme)

    def test_docs_require_tmux_launcher_and_status_dashboard_examples(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (skill, readme):
            self.assertIn("launch-exec", document)
            self.assertIn("tmux", document)
            self.assertIn("status --repo", document)
            self.assertIn("--watch", document)
            self.assertIn("--json", document)
            self.assertIn("tmux attach -t", document)

    def test_docs_define_low_intrusion_local_worker_monitoring(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (skill, readme):
            self.assertIn("immediately after launch", document)
            self.assertIn("standalone", document)
            self.assertIn("watch-exec", document)
            self.assertIn("--seconds 60", document)
            self.assertIn("five seconds", document)
            self.assertIn("never use shell `sleep`", document)
            self.assertIn("compound commands", document)
            self.assertIn("status --watch", document)

        self.assertIn("SUCCEEDED_AWAITING_REVIEW", skill)
        self.assertIn("NEEDS_ATTENTION", skill)
        self.assertIn("STALE", skill)
        self.assertIn("UNKNOWN", skill)

        for terminal_status in (
            "SUCCEEDED_AWAITING_REVIEW",
            "NEEDS_ATTENTION",
            "STALE",
            "UNKNOWN",
        ):
            self.assertIn(terminal_status, readme)

        self.assertIn("manual `status --json` polling", readme)
        self.assertIn("read-only", readme)

    def test_docs_require_bounded_watch_exec_checkpoints_for_controllers(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (skill, readme):
            self.assertIn("watch-exec", document)
            self.assertIn("--seconds", document)
            self.assertIn("checkpoint", document)
            self.assertIn("status --watch", document)

    def test_docs_require_reconciliation_before_a_controller_can_go_idle(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (skill, readme):
            self.assertIn("reconcile", document)
            self.assertIn("supervise", document)
            self.assertIn("No change", document)

    def test_docs_define_guarded_delivery_policy(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (skill, readme):
            self.assertIn("no-mistakes", document)
            self.assertIn("direct-pr", document)
            self.assertIn("local-only", document)
            self.assertIn("+yolo", document)
            self.assertIn("explicit discard", document)

        self.assertIn("must not be the controller checkout", skill)
        self.assertIn("delivery-ready", skill)
        self.assertIn("UNKNOWN", skill)

    def test_readme_explains_why_guarded_delivery_exists(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(
            "the safe handoff from an isolated task worktree to delivery",
            readme,
        )
        self.assertIn("Choose how the completed work should be delivered", readme)
        self.assertIn("Record delivery before cleaning up", readme)

    def test_readme_is_codex_first_guided_reference(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        headings = (
            "## What it is",
            "## Features",
            "## Quick Start",
            "## How It Works",
            "## Guarded Delivery",
            "## Command Reference",
        )

        for heading in headings:
            self.assertIn(heading, readme)
        self.assertLess(readme.index("## Quick Start"), readme.index("Install for Claude Code"))
        self.assertLess(readme.index("## Guarded Delivery"), readme.index("## Command Reference"))

    def test_docs_describe_the_tmux_resident_local_controller_boundary(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (skill, readme):
            self.assertIn("controller.py start", document)
            self.assertIn("controller.py status", document)
            self.assertIn("controller.py stop", document)
            self.assertIn("controller.json", document)
        self.assertIn("tmux-resident", skill)
        self.assertIn("Stop hook is a blind-end backstop", skill)


if __name__ == "__main__":
    unittest.main()
