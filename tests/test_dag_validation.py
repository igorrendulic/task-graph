import json
import tempfile
import unittest
from pathlib import Path

from scripts.dag_validation import DagValidationError, validate_dag, validate_dag_file


def valid_dag():
    return {
        "schemaVersion": 1,
        "planSlug": "example-plan",
        "tasks": [
            {
                "id": "001-schema",
                "taskFile": "001-schema.md",
                "title": "Add schema",
                "instructions": "Implement the schema.",
                "predictedPaths": ["src/schema.py"],
                "predictedSymbols": ["Schema"],
                "dependsOn": [],
                "parallelSafe": True,
                "schedulingRationale": "The predicted surface is disjoint.",
            },
            {
                "id": "002-api",
                "taskFile": "002-api.md",
                "title": "Add API",
                "instructions": "Implement the API.",
                "predictedPaths": ["src/api.py"],
                "predictedSymbols": ["create_api"],
                "dependsOn": ["001-schema"],
                "parallelSafe": False,
                "schedulingRationale": "The API consumes the schema contract.",
            },
        ],
    }


class ValidateDagTests(unittest.TestCase):
    def test_accepts_valid_dag_and_matching_task_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            plan_dir = Path(temp) / "example-plan"
            todo = plan_dir / "todo"
            todo.mkdir(parents=True)
            (todo / "001-schema.md").write_text("# Schema\n\n## Dependencies\n\nNone\n")
            (todo / "002-api.md").write_text(
                "# API\n\n## Dependencies\n\n- 001-schema.md\n"
            )
            dag_path = plan_dir / "dag.json"
            dag_path.write_text(json.dumps(valid_dag()))

            validate_dag_file(dag_path, plan_dir)

    def test_rejects_unknown_dependency(self):
        dag = valid_dag()
        dag["tasks"][1]["dependsOn"] = ["999-missing"]

        with self.assertRaisesRegex(DagValidationError, "unknown task ID"):
            validate_dag(dag)

    def test_rejects_cycle(self):
        dag = valid_dag()
        dag["tasks"][0]["dependsOn"] = ["002-api"]

        with self.assertRaisesRegex(DagValidationError, "cycle"):
            validate_dag(dag)

    def test_rejects_task_file_dependency_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            plan_dir = Path(temp) / "example-plan"
            todo = plan_dir / "todo"
            todo.mkdir(parents=True)
            (todo / "001-schema.md").write_text("# Schema\n\n## Dependencies\n\nNone\n")
            (todo / "002-api.md").write_text("# API\n\n## Dependencies\n\nNone\n")
            dag_path = plan_dir / "dag.json"
            dag_path.write_text(json.dumps(valid_dag()))

            with self.assertRaisesRegex(DagValidationError, "do not match"):
                validate_dag_file(dag_path, plan_dir)

    def test_rejects_duplicate_ids(self):
        dag = valid_dag()
        dag["tasks"][1]["id"] = dag["tasks"][0]["id"]

        with self.assertRaisesRegex(DagValidationError, "duplicate task ID"):
            validate_dag(dag)

    def test_workspace_dag_requires_declared_project_and_safe_project_relative_paths(self):
        dag = valid_dag()
        dag["schemaVersion"] = 2
        dag["tasks"][0]["project"] = "frontend"
        dag["tasks"][1]["project"] = "api"

        validate_dag(dag, workspace_project_ids={"frontend", "api"})

        dag["tasks"][1]["project"] = "undeclared"
        with self.assertRaisesRegex(DagValidationError, "declared workspace project"):
            validate_dag(dag, workspace_project_ids={"frontend", "api"})

        dag["tasks"][1]["project"] = "api"
        dag["tasks"][1]["predictedPaths"] = ["../other/project.py"]
        with self.assertRaisesRegex(DagValidationError, "project-relative"):
            validate_dag(dag, workspace_project_ids={"frontend", "api"})
