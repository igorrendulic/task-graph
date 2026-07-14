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

        self.assertIn("Select an execution mode before reserving", skill)
        self.assertIn("Managed workers (default)", skill)
        self.assertIn("Unattended `codex exec`", skill)
        self.assertIn("Cloud delegation", skill)
        self.assertIn("process identifier", skill)
        self.assertIn("awake machine or remote host", skill)
        self.assertIn("Execution Modes", readme)
        self.assertIn("Unattended `codex exec`", readme)
        self.assertIn("not laptop-independent", readme)

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


if __name__ == "__main__":
    unittest.main()
