import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.dag_case_evaluator import evaluate_case


class EvaluateDagCaseTests(unittest.TestCase):
    def test_cli_runs_as_a_script(self):
        result = subprocess.run(
            [sys.executable, "scripts/dag_case_evaluator.py", "--help"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_accepts_artifacts_that_match_the_behavior_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case_dir = root / "case"
            artifacts_dir = root / "disjoint-plan"
            case_dir.mkdir()
            _write_case(case_dir, True)
            _write_artifacts(artifacts_dir, True)

            self.assertEqual([], evaluate_case(case_dir, artifacts_dir))

    def test_reports_parallel_safety_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case_dir = root / "case"
            artifacts_dir = root / "disjoint-plan"
            case_dir.mkdir()
            _write_case(case_dir, True)
            _write_artifacts(artifacts_dir, False)

            errors = evaluate_case(case_dir, artifacts_dir)

            self.assertTrue(any("parallelSafe" in error for error in errors))


def _write_case(case_dir: Path, parallel_safe: bool) -> None:
    (case_dir / "expected.json").write_text(
        json.dumps(
            {
                "planSlug": "disjoint-plan",
                "tasks": {
                    "001-schema": {
                        "dependsOn": [],
                        "parallelSafe": parallel_safe,
                        "rationaleContains": ["disjoint"],
                    }
                },
            }
        )
    )


def _write_artifacts(artifacts_dir: Path, parallel_safe: bool) -> None:
    todo = artifacts_dir / "todo"
    todo.mkdir(parents=True)
    (todo / "001-schema.md").write_text("# Schema\n\n## Dependencies\n\nNone\n")
    (artifacts_dir / "dag.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "planSlug": "disjoint-plan",
                "tasks": [
                    {
                        "id": "001-schema",
                        "taskFile": "001-schema.md",
                        "title": "Add schema",
                        "instructions": "Implement the schema.",
                        "predictedPaths": ["src/schema.py"],
                        "predictedSymbols": ["Schema"],
                        "dependsOn": [],
                        "parallelSafe": parallel_safe,
                        "schedulingRationale": "Its predicted surface is disjoint.",
                    }
                ],
            }
        )
    )
