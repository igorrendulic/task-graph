import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CODEX_FILES = (
    "SKILL.md",
    "scripts/kanban.py",
    "scripts/controller.py",
    "scripts/watcher.py",
    "agents/openai.yaml",
)


class InstallerTest(unittest.TestCase):
    def assert_codex_install_contains_required_payload(self, command: list[str]) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex"
            result = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
                text=True,
                capture_output=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            target = codex_home / "skills" / "task-graph"
            for relative_path in REQUIRED_CODEX_FILES:
                self.assertTrue((target / relative_path).is_file(), relative_path)

    def test_node_installer_copies_complete_codex_payload(self) -> None:
        self.assert_codex_install_contains_required_payload(
            ["node", "bin/task-graph-skill.js", "install", "--codex-only"]
        )

    def test_shell_installer_copies_complete_codex_payload(self) -> None:
        self.assert_codex_install_contains_required_payload(
            ["sh", "install.sh", "--codex-only"]
        )

    def test_shell_link_installer_replaces_existing_skill_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex"
            env = {**os.environ, "CODEX_HOME": str(codex_home)}
            target = codex_home / "skills" / "task-graph"
            target.mkdir(parents=True)
            (target / "stale.txt").write_text("stale\n", encoding="utf-8")

            result = subprocess.run(
                ["sh", "install.sh", "--codex-only", "--link", "--force"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(target.is_symlink())
            self.assertEqual(ROOT, target.resolve())


if __name__ == "__main__":
    unittest.main()
