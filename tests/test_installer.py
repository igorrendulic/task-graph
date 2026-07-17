import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "install.sh"
BASH = shutil.which("bash")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.codex_home = self.root / "codex-home"
        self.mock_bin = self.root / "mock-bin"
        self.mock_bin.mkdir()
        self.archive_source = self.root / "archive-source"
        self._write_skill_tree(self.archive_source)
        self.command_log = self.root / "commands.log"
        self._write_mock_commands()

    def tearDown(self):
        self.temp_directory.cleanup()

    def _write_skill_tree(self, root: Path) -> None:
        (root / "scripts").mkdir(parents=True)
        (root / "references").mkdir()
        (root / "agents").mkdir()
        (root / "SKILL.md").write_text("# Installed skill\n")
        (root / "scripts" / "runner.py").write_text("print('runner')\n")
        (root / "references" / "guide.md").write_text("guide\n")
        (root / "agents" / "openai.yaml").write_text("agent: test\n")

    def _write_executable(self, name: str, content: str) -> None:
        command = self.mock_bin / name
        command.write_text(content)
        command.chmod(0o755)

    def _write_mock_commands(self):
        self._write_executable(
            "curl",
            """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$MOCK_COMMAND_LOG"
if [ "${MOCK_CURL_FAIL:-0}" = "1" ]; then
  exit 23
fi
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    cp "$MOCK_ARCHIVE_SOURCE" "$2"
    exit 0
  fi
  shift
done
exit 2
""",
        )
        self._write_executable(
            "tar",
            """#!/bin/sh
printf 'tar %s\\n' "$*" >> "$MOCK_COMMAND_LOG"
if [ "${MOCK_TAR_FAIL:-0}" = "1" ]; then
  exit 2
fi
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-C" ]; then
    destination="$2"
    break
  fi
  shift
done
mkdir -p "$destination/task-graph-fixture"
if [ "${MOCK_MALFORMED_ARCHIVE:-0}" = "1" ]; then
  mkdir -p "$destination/task-graph-fixture/scripts" "$destination/task-graph-fixture/references"
  printf '# Incomplete skill\\n' > "$destination/task-graph-fixture/SKILL.md"
  exit 0
fi
cp -R "$MOCK_ARCHIVE_SOURCE"/. "$destination/task-graph-fixture/"
""",
        )

    def _run_installer(self, *arguments: str, path: str | None = None, **environment):
        command_environment = {
            **os.environ,
            "CODEX_HOME": str(self.codex_home),
            "MOCK_ARCHIVE_SOURCE": str(self.archive_source),
            "MOCK_COMMAND_LOG": str(self.command_log),
            "PATH": path or f"{self.mock_bin}:{os.environ['PATH']}",
            **environment,
        }
        return subprocess.run(
            [BASH, str(INSTALLER), *arguments],
            capture_output=True,
            text=True,
            env=command_environment,
        )

    @property
    def target(self) -> Path:
        return self.codex_home / "skills" / "task-graph"

    def _write_existing_install(self):
        self.target.mkdir(parents=True)
        (self.target / "existing.txt").write_text("preserve me\n")

    def _write_dangling_install_link(self):
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(self.root / "missing-install")

    def test_clean_install_copies_the_required_skill_tree(self):
        result = self._run_installer()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("# Installed skill\n", (self.target / "SKILL.md").read_text())
        self.assertTrue((self.target / "scripts" / "runner.py").is_file())
        self.assertTrue((self.target / "references" / "guide.md").is_file())
        self.assertTrue((self.target / "agents" / "openai.yaml").is_file())

    def test_existing_install_requires_force(self):
        self._write_existing_install()

        result = self._run_installer()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--force", result.stderr)
        self.assertEqual("preserve me\n", (self.target / "existing.txt").read_text())

    def test_dangling_install_link_requires_force(self):
        self._write_dangling_install_link()

        result = self._run_installer()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--force", result.stderr)
        self.assertTrue(self.target.is_symlink())

    def test_force_replaces_a_dangling_install_link(self):
        self._write_dangling_install_link()

        result = self._run_installer("--force")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(self.target.is_symlink())
        self.assertTrue((self.target / "SKILL.md").is_file())

    def test_force_replaces_an_existing_install_only_after_staging_succeeds(self):
        self._write_existing_install()

        failed_result = self._run_installer("--force", MOCK_CURL_FAIL="1")

        self.assertNotEqual(0, failed_result.returncode)
        self.assertEqual("preserve me\n", (self.target / "existing.txt").read_text())

        extraction_failed_result = self._run_installer("--force", MOCK_TAR_FAIL="1")

        self.assertNotEqual(0, extraction_failed_result.returncode)
        self.assertEqual("preserve me\n", (self.target / "existing.txt").read_text())

        result = self._run_installer("--force")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.target / "existing.txt").exists())
        self.assertTrue((self.target / "SKILL.md").is_file())

    def test_ref_is_used_in_the_github_archive_url(self):
        result = self._run_installer("--ref", "v1.2.3")

        self.assertEqual(0, result.returncode, result.stderr)
        log = self.command_log.read_text()
        self.assertIn(
            "https://github.com/igorrendulic/task-graph/archive/v1.2.3.tar.gz", log
        )

    def test_ref_rejects_an_empty_or_option_value(self):
        empty_value = self._run_installer("--ref", "")
        option_value = self._run_installer("--ref", "--force")

        self.assertNotEqual(0, empty_value.returncode)
        self.assertIn("--ref requires", empty_value.stderr)
        self.assertNotEqual(0, option_value.returncode)
        self.assertIn("--ref requires", option_value.stderr)

    def test_missing_tools_invalid_arguments_and_bad_archives_leave_existing_install_untouched(
        self,
    ):
        self._write_existing_install()
        missing_tool_path = str(self.root / "empty-path")
        Path(missing_tool_path).mkdir()

        missing_tool = self._run_installer(path=missing_tool_path)
        unknown_argument = self._run_installer("--unknown")
        missing_ref = self._run_installer("--ref")
        malformed_archive = self._run_installer(
            "--force", MOCK_MALFORMED_ARCHIVE="1"
        )

        self.assertNotEqual(0, missing_tool.returncode)
        self.assertIn("curl", missing_tool.stderr)
        self.assertNotEqual(0, unknown_argument.returncode)
        self.assertIn("Usage", unknown_argument.stderr)
        self.assertNotEqual(0, missing_ref.returncode)
        self.assertIn("--ref", missing_ref.stderr)
        self.assertNotEqual(0, malformed_archive.returncode)
        self.assertIn("agents", malformed_archive.stderr)
        self.assertEqual("preserve me\n", (self.target / "existing.txt").read_text())


if __name__ == "__main__":
    unittest.main()
