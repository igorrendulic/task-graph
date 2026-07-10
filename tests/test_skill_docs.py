import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillDocsTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
