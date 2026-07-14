import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillDocsTest(unittest.TestCase):
    def test_docs_require_a_plan_scoped_board(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(".agent/<plan-slug>/kanban.md", skill)
        self.assertIn("--plan <plan-slug>", skill)
        self.assertIn("never reads or updates the legacy shared", skill)
        self.assertIn(".agent/<plan-slug>/", readme)
        self.assertIn("--plan <plan-slug>", readme)

    def test_failed_audit_requires_user_checkpoint_before_another_loop(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Outcome improvement checkpoints", skill)
        self.assertIn("failed audit or verification report", skill)
        self.assertIn("Do not create, reserve, dispatch, or run another improvement loop", skill)
        self.assertIn("`Stop`", skill)
        self.assertIn("`Continue`", skill)

    def test_readme_documents_improvement_loop_checkpoint(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Improvement Loop Checkpoints", readme)
        self.assertIn("After each failed audit", readme)
        self.assertIn("stop with the current unresolved result", readme)
        self.assertIn("continue into another focused improvement-and-audit loop", readme)

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
            self.assertIn("platform-native wait", document)
            self.assertIn("60 seconds", document)
            self.assertIn("standalone", document)
            self.assertIn("status --json", document)
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

        self.assertIn("Approval is needed only for the standalone status-command prefix", readme)
        self.assertIn("never for an artificial delay command", readme)
        self.assertLess(
            readme.index("immediately after launch"),
            readme.index("platform-native wait of 60 seconds"),
        )
        self.assertLess(
            readme.index("platform-native wait of 60 seconds"),
            readme.index("before every later probe"),
        )

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


if __name__ == "__main__":
    unittest.main()
