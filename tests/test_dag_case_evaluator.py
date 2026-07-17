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

    def test_matches_tasks_by_title_and_translates_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case_dir = root / "case"
            artifacts_dir = root / "shared-file"
            case_dir.mkdir()
            _write_semantic_case(case_dir)
            _write_semantic_artifacts(artifacts_dir)

            self.assertEqual([], evaluate_case(case_dir, artifacts_dir))

    def test_reports_ambiguous_title_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case_dir = root / "case"
            artifacts_dir = root / "shared-file"
            case_dir.mkdir()
            _write_semantic_case(case_dir, title_contains="configuration")
            _write_semantic_artifacts(artifacts_dir)

            errors = evaluate_case(case_dir, artifacts_dir)

            self.assertEqual(
                [
                    "001-add-parser.titleContains matched multiple generated tasks: "
                    "001-add-config-parser, 002-add-config-serializer"
                ],
                errors,
            )

    def test_reports_unmatched_generated_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case_dir = root / "case"
            artifacts_dir = root / "shared-file"
            case_dir.mkdir()
            _write_semantic_case(case_dir)
            _write_semantic_artifacts(artifacts_dir, include_extra_task=True)

            self.assertEqual(
                ["generated task IDs were not matched by the case: 003-add-config-linter"],
                evaluate_case(case_dir, artifacts_dir),
            )

    def test_rejects_null_title_matcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case_dir = root / "case"
            artifacts_dir = root / "shared-file"
            case_dir.mkdir()
            _write_semantic_case(case_dir, title_contains=None)
            _write_semantic_artifacts(artifacts_dir)

            self.assertEqual(
                ["001-add-parser.titleContains must be a non-empty string"],
                evaluate_case(case_dir, artifacts_dir),
            )


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


def _write_semantic_case(
    case_dir: Path, title_contains: str | None = "configuration parser"
) -> None:
    (case_dir / "expected.json").write_text(
        json.dumps(
            {
                "planSlug": "shared-file",
                "tasks": {
                    "001-add-parser": {
                        "titleContains": title_contains,
                        "dependsOn": [],
                        "parallelSafe": False,
                        "rationaleContains": ["shared"],
                    },
                    "002-add-serializer": {
                        "titleContains": "configuration serializer",
                        "dependsOn": ["001-add-parser"],
                        "parallelSafe": False,
                        "rationaleContains": ["shared"],
                    },
                },
            }
        )
    )


def _write_semantic_artifacts(artifacts_dir: Path, include_extra_task: bool = False) -> None:
    todo = artifacts_dir / "todo"
    todo.mkdir(parents=True)
    (todo / "001-add-config-parser.md").write_text("# Parser\n\n## Dependencies\n\nNone\n")
    (todo / "002-add-config-serializer.md").write_text(
        "# Serializer\n\n## Dependencies\n\n- `001-add-config-parser.md`\n"
    )
    if include_extra_task:
        (todo / "003-add-config-linter.md").write_text("# Linter\n\n## Dependencies\n\nNone\n")
    tasks = [
        {
            "id": "001-add-config-parser",
            "taskFile": "001-add-config-parser.md",
            "title": "Add configuration parser",
            "instructions": "Implement the parser.",
            "predictedPaths": ["src/config.py"],
            "predictedSymbols": ["parse_config"],
            "dependsOn": [],
            "parallelSafe": False,
            "schedulingRationale": "It uses the shared src/config.py module.",
        },
        {
            "id": "002-add-config-serializer",
            "taskFile": "002-add-config-serializer.md",
            "title": "Add configuration serializer",
            "instructions": "Implement the serializer.",
            "predictedPaths": ["src/config.py"],
            "predictedSymbols": ["serialize_config"],
            "dependsOn": ["001-add-config-parser"],
            "parallelSafe": False,
            "schedulingRationale": "It uses the shared src/config.py module.",
        },
    ]
    if include_extra_task:
        tasks.append(
            {
                "id": "003-add-config-linter",
                "taskFile": "003-add-config-linter.md",
                "title": "Add configuration linter",
                "instructions": "Implement the linter.",
                "predictedPaths": ["src/config.py"],
                "predictedSymbols": ["lint_config"],
                "dependsOn": [],
                "parallelSafe": False,
                "schedulingRationale": "It uses the shared src/config.py module.",
            }
        )
    (artifacts_dir / "dag.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "planSlug": "shared-file",
                "tasks": tasks,
            }
        )
    )
