"""Workspace manifest validation for multi-project Task Graph runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.task_graph_git import TaskGraphGit, TaskGraphGitError


class WorkspaceError(ValueError):
    """Raised when a workspace manifest is unsafe or invalid."""


@dataclass(frozen=True)
class WorkspaceProject:
    id: str
    path: Path


@dataclass(frozen=True)
class Workspace:
    root: Path
    projects: tuple[WorkspaceProject, ...]

    @property
    def by_id(self) -> dict[str, WorkspaceProject]:
        return {project.id: project for project in self.projects}


def load_workspace(path: Path) -> Workspace:
    """Load a manifest from a workspace root or its manifest file.

    Project paths are deliberately resolved only after rejecting absolute and
    parent-traversing values.  A declared execution project must be the root of
    its own Git checkout, which prevents a manifest from accidentally using a
    parent repository or an undeclared sibling directory.
    """
    candidate = path.resolve()
    manifest = candidate if candidate.name == "task-graph.workspace.json" else candidate / ".agent" / "task-graph.workspace.json"
    if not manifest.is_file():
        raise WorkspaceError(f"workspace manifest does not exist: {manifest}")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"cannot read workspace manifest: {exc}") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise WorkspaceError("workspace schemaVersion must be 1")
    entries = value.get("projects")
    if not isinstance(entries, list) or not entries:
        raise WorkspaceError("workspace projects must be a non-empty array")
    root = manifest.parent.parent.resolve()
    projects: list[WorkspaceProject] = []
    ids: set[str] = set()
    paths: set[Path] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise WorkspaceError(f"projects[{index}] must be an object")
        project_id, raw_path = entry.get("id"), entry.get("path")
        if not isinstance(project_id, str) or not project_id.strip():
            raise WorkspaceError(f"projects[{index}].id must be a non-empty string")
        if project_id in ids:
            raise WorkspaceError(f"duplicate workspace project ID: {project_id}")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WorkspaceError(f"projects[{index}].path must be a non-empty relative path")
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise WorkspaceError(f"projects[{index}].path must be a safe relative path")
        project_path = (root / relative).resolve()
        if root not in project_path.parents:
            raise WorkspaceError(f"projects[{index}].path escapes the workspace root")
        if project_path in paths:
            raise WorkspaceError(f"duplicate workspace project path: {raw_path}")
        if not project_path.is_dir():
            raise WorkspaceError(f"workspace project does not exist: {project_path}")
        try:
            git_root = TaskGraphGit.repository_root(project_path)
        except TaskGraphGitError as exc:
            raise WorkspaceError(f"workspace project is not a Git checkout: {project_path}") from exc
        if git_root != project_path:
            raise WorkspaceError(
                f"workspace project must be an independent Git root: {project_path} (root is {git_root})"
            )
        ids.add(project_id)
        paths.add(project_path)
        projects.append(WorkspaceProject(project_id, project_path))
    return Workspace(root, tuple(projects))
