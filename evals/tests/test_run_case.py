import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from evals.run_case import _CODEX_PROMPT, _run_codex, materialize_case, run_case


CASES = Path("evals/cases")


class MaterializeDagEvalCaseTests(unittest.TestCase):
    def test_generation_prompt_requires_task_file_basenames(self):
        self.assertIn("Each taskFile must be only its .md filename", _CODEX_PROMPT)
        self.assertIn("not a path", _CODEX_PROMPT)

    def test_generation_prompt_requires_plan_slug_to_match_artifact_directory(self):
        self.assertIn("root-level planSlug", _CODEX_PROMPT)
        self.assertRegex(
            _CODEX_PROMPT,
            r"(?s)root-level planSlug must be non-empty.*equal the "
            r"\.agent/<plan-slug>/\s*directory name",
        )

    def test_generation_prompt_maps_dag_ids_to_task_file_dependency_filenames(self):
        self.assertIn("dag.json dependsOn arrays contain task IDs", _CODEX_PROMPT)
        self.assertRegex(
            _CODEX_PROMPT,
            r"## Dependencies section contains the matching taskFile\s+\.md filenames",
        )
        self.assertRegex(
            _CODEX_PROMPT,
            r"validate every task-file dependency against the referenced task's\s+taskFile",
        )
        self.assertIn(
            "correct the artifacts and revalidate before reporting success", _CODEX_PROMPT
        )

    def test_generation_prompt_requires_shared_scheduling_rationales(self):
        self.assertRegex(
            _CODEX_PROMPT,
            r"Each schedulingRationale must name the basis for serialization",
        )
        self.assertIn("shared", _CODEX_PROMPT)
        self.assertRegex(
            _CODEX_PROMPT,
            r"both\s+tasks modify the shared `src/config\.py` module and configuration tests",
        )

    def test_creates_a_clean_git_repository_from_a_case_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            repo_dir = materialize_case(CASES / "001-disjoint", Path(temp) / "repo")

            self.assertTrue((repo_dir / ".git").is_dir())
            self.assertTrue((repo_dir / "src").is_dir())
            self.assertTrue((repo_dir / "plan.md").is_file())
            self.assertEqual("", _git(repo_dir, "status", "--porcelain"))

    def test_applies_declared_dirty_changes_after_the_baseline_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            repo_dir = materialize_case(CASES / "004-dirty-overlap", Path(temp) / "repo")

            self.assertIn("src/settings.py", _git(repo_dir, "status", "--porcelain"))
            self.assertIn("existing local edit", (repo_dir / "src/settings.py").read_text())

    def test_materialized_fixtures_expose_their_scheduling_surfaces(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            disjoint = materialize_case(CASES / "001-disjoint", root / "disjoint")
            core = _load_fixture_module(disjoint, "core")
            self.assertEqual("Ada", core.new_record("customer-1", "Ada").display_name)
            self.assertFalse((disjoint / "src" / "schema.py").exists())
            self.assertFalse((disjoint / "src" / "formatter.py").exists())

            shared = materialize_case(CASES / "002-shared-file", root / "shared")
            config = _load_fixture_module(shared, "config")
            self.assertEqual(8080, config.load_config({"PORT": "8080"}).port)

            uncertain = materialize_case(CASES / "003-uncertain-overlap", root / "uncertain")
            cache = _load_fixture_module(uncertain, "cache")
            memory = cache.MemoryCache({"customer-1": "cached"})
            self.assertTrue(cache.CacheInvalidator(memory).invalidate("customer-1"))
            self.assertIsNone(memory.get("customer-1"))

            dirty = materialize_case(CASES / "004-dirty-overlap", root / "dirty")
            settings = _load_fixture_module(dirty, "settings")
            self.assertEqual(8080, settings.validate_port("8080"))
            with self.assertRaises(ValueError):
                settings.validate_port("0")
            self.assertIn("# existing local edit", (dirty / "src" / "settings.py").read_text())

    @patch("evals.run_case._run_codex")
    def test_runs_codex_then_prints_and_scores_generated_dag(self, run_codex):
        def write_generated_artifacts(repo_dir: Path, codex_bin: str) -> list[str]:
            artifacts = repo_dir / ".agent" / "disjoint-changes"
            todo = artifacts / "todo"
            todo.mkdir(parents=True)
            (todo / "001-add-schema.md").write_text("# Schema\n\n## Dependencies\n\nNone\n")
            (todo / "002-add-formatter.md").write_text("# Formatter\n\n## Dependencies\n\nNone\n")
            (artifacts / "dag.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "planSlug": "disjoint-changes",
                        "tasks": [
                            _task("001-add-schema", "001-add-schema.md", "src/schema.py"),
                            _task("002-add-formatter", "002-add-formatter.md", "src/formatter.py"),
                        ],
                    }
                )
            )
            return []

        run_codex.side_effect = write_generated_artifacts
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()) as output:
            errors = run_case(CASES / "001-disjoint", Path(temp) / "repo")

        self.assertEqual([], errors)
        self.assertIn('"planSlug": "disjoint-changes"', output.getvalue())
        run_codex.assert_called_once()

    @patch("evals.run_case._run_codex")
    def test_run_case_without_repo_uses_cleaned_temporary_repository(self, run_codex):
        generated_repo_dirs: list[Path] = []

        def write_generated_artifacts(repo_dir: Path, codex_bin: str) -> list[str]:
            generated_repo_dirs.append(repo_dir)
            artifacts = repo_dir / ".agent" / "disjoint-changes"
            todo = artifacts / "todo"
            todo.mkdir(parents=True)
            (todo / "001-add-schema.md").write_text("# Schema\n\n## Dependencies\n\nNone\n")
            (todo / "002-add-formatter.md").write_text("# Formatter\n\n## Dependencies\n\nNone\n")
            (artifacts / "dag.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "planSlug": "disjoint-changes",
                        "tasks": [
                            _task("001-add-schema", "001-add-schema.md", "src/schema.py"),
                            _task("002-add-formatter", "002-add-formatter.md", "src/formatter.py"),
                        ],
                    }
                )
            )
            return []

        run_codex.side_effect = write_generated_artifacts
        with redirect_stdout(io.StringIO()):
            errors = run_case(CASES / "001-disjoint")

        self.assertEqual([], errors)
        self.assertEqual(1, len(generated_repo_dirs))
        self.assertFalse(generated_repo_dirs[0].exists())

    @patch("evals.run_case._run_codex", return_value=[])
    def test_reports_when_codex_exits_without_a_dag(self, run_codex):
        with tempfile.TemporaryDirectory() as temp:
            errors = run_case(CASES / "001-disjoint", Path(temp) / "repo")

        self.assertEqual(1, len(errors))
        self.assertIn("without producing the expected DAG", errors[0])
        self.assertIn("Inspect", errors[0])
        run_codex.assert_called_once()

    @patch("evals.run_case._run_codex", return_value=[])
    def test_ephemeral_missing_dag_error_does_not_reference_deleted_repo(self, run_codex):
        errors = run_case(CASES / "001-disjoint")

        self.assertEqual(1, len(errors))
        self.assertIn("without producing the expected DAG", errors[0])
        self.assertNotIn("Inspect", errors[0])
        self.assertNotIn("codex-output.txt", errors[0])
        run_codex.assert_called_once()

    @patch("evals.run_case.subprocess.Popen")
    def test_streams_codex_output_and_saves_the_transcript(self, popen):
        process = popen.return_value
        process.stdout = ["planning tasks\n", "writing dag.json\n"]
        process.wait.return_value = 0

        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()) as output:
            repo_dir = Path(temp)
            errors = _run_codex(repo_dir, "missing-codex")

            transcript = (repo_dir / "codex-output.txt").read_text()

        self.assertEqual([], errors)
        self.assertEqual("planning tasks\nwriting dag.json\n", output.getvalue())
        self.assertIn("planning tasks", transcript)


def _git(repo_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout.strip()


def _load_fixture_module(repo_dir: Path, module_name: str):
    module_path = repo_dir / "src" / f"{module_name}.py"
    unique_name = f"fixture_{repo_dir.name}_{module_name}"
    spec = importlib.util.spec_from_file_location(unique_name, module_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import fixture module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _task(task_id: str, task_file: str, path: str) -> dict[str, object]:
    return {
        "id": task_id,
        "taskFile": task_file,
        "title": task_id,
        "instructions": "Implement the task.",
        "predictedPaths": [path],
        "predictedSymbols": [],
        "dependsOn": [],
        "parallelSafe": True,
        "schedulingRationale": "The predicted surface is disjoint.",
    }
