import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.task_graph_controller import TaskGraphController
from scripts.task_graph_runtime import create_run_snapshot, load_state, write_state
from scripts.task_graph_tmux import PaneInfo
from scripts.task_graph_workspace import WorkspaceError, load_workspace


def _repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    (path / "base.txt").write_text("base")
    subprocess.run(["git", "add", "base.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=path, check=True)


class _Tmux:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def create_window(self, session: str, name: str, cwd: Path, command: str) -> str:
        self.commands.append(command)
        return "%1"

    def pane_info(self, pane_id: str) -> PaneInfo:
        return PaneInfo(pane_id, 1)


class WorkspaceManifestTests(unittest.TestCase):
    def test_loads_any_number_of_independent_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("frontend", "api", "worker"):
                _repo(root / "projects" / name)
            (root / ".agent").mkdir()
            (root / ".agent" / "task-graph.workspace.json").write_text(json.dumps({
                "schemaVersion": 1,
                "projects": [
                    {"id": "frontend", "path": "projects/frontend"},
                    {"id": "api", "path": "projects/api"},
                    {"id": "worker", "path": "projects/worker"},
                ],
            }))

            workspace = load_workspace(root)

            self.assertEqual(["frontend", "api", "worker"], [p.id for p in workspace.projects])

    def test_rejects_unsafe_or_non_root_project_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _repo(root / "project")
            (root / ".agent").mkdir()
            manifest = root / ".agent" / "task-graph.workspace.json"
            manifest.write_text(json.dumps({"schemaVersion": 1, "projects": [{"id": "bad", "path": "../project"}]}))
            with self.assertRaisesRegex(WorkspaceError, "safe relative"):
                load_workspace(root)

            (root / "project" / "nested").mkdir()
            manifest.write_text(json.dumps({"schemaVersion": 1, "projects": [{"id": "nested", "path": "project/nested"}]}))
            with self.assertRaisesRegex(WorkspaceError, "independent Git root"):
                load_workspace(root)

    def test_workspace_controller_runs_independent_projects_and_exposes_integrated_contract_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            api, frontend = root / "api", root / "frontend"
            _repo(api)
            _repo(frontend)
            plan = root / ".agent" / "demo"
            (plan / "todo").mkdir(parents=True)
            (plan / "todo" / "001-api.md").write_text("# API\n\n## Dependencies\n\nNone\n")
            (plan / "todo" / "002-frontend.md").write_text("# Frontend\n\n## Dependencies\n\n- 001-api.md\n")
            (plan / "dag.json").write_text(json.dumps({"schemaVersion": 2, "planSlug": "demo", "tasks": [
                {"id": "001-api", "project": "api", "taskFile": "001-api.md", "title": "API", "instructions": "API", "predictedPaths": ["contract.ts"], "predictedSymbols": [], "dependsOn": [], "parallelSafe": True, "schedulingRationale": "disjoint"},
                {"id": "002-frontend", "project": "frontend", "taskFile": "002-frontend.md", "title": "Frontend", "instructions": "Frontend", "predictedPaths": ["page.tsx"], "predictedSymbols": [], "dependsOn": ["001-api"], "parallelSafe": True, "schedulingRationale": "shared contract"},
            ]}))
            run = plan / "runs" / "run-1"
            snapshot = create_run_snapshot(plan, run, workspace_project_ids={"api", "frontend"})
            projects = {}
            for project_id, repository in (("api", api), ("frontend", frontend)):
                branch = f"task-graph/demo/run-1/{project_id}/feature"
                integration = run / "projects" / project_id / "integration"
                subprocess.run(["git", "branch", branch], cwd=repository, check=True)
                subprocess.run(["git", "worktree", "add", "--quiet", str(integration), branch], cwd=repository, check=True)
                common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
                projects[project_id] = {"repository": str(repository), "gitCommonDir": common, "integrationWorktree": str(integration), "used": True}
            state = {"schemaVersion": 1, "workspace": str(root), "runId": "run-1", "planSlug": "demo", "planDirectory": str(plan), "projects": projects, "dagDigest": snapshot.dag_digest, "taskDigests": snapshot.task_digests, "maxWorkers": 2, "workerCommand": "codex", "session": "session", "controller": {}, "tasks": {"001-api": {"status": "integrated", "attempts": [], "commitSha": "api-sha"}, "002-frontend": {"status": "pending", "attempts": [], "commitSha": None}}}
            write_state(run, state)

            controller = TaskGraphController(run, tmux=_Tmux())
            prompt = controller.build_worker_prompt("002-frontend")

            self.assertIn("project=api", prompt)
            self.assertIn("integratedCommit=api-sha", prompt)
            self.assertIn(str(run / "projects" / "api" / "integration"), prompt)
