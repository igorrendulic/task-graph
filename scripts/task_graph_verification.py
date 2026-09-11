"""Controller-owned verification of the exact worker commit before integration."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.dag_validation import DagValidationError, validate_verification
from scripts.task_graph_git import TaskGraphGit, TaskGraphGitError


def require_execution_contract(task: dict[str, Any]) -> None:
    validate_verification(task.get("verification"))
    for path in task["predictedPaths"]:
        parts = PurePosixPath(path).parts
        if not path or path.startswith('/') or not parts or '..' in parts or any(c in path for c in '*?[]\\\0'):
            raise DagValidationError("execution requires concrete project-relative predictedPaths (files or directory/ prefixes)")


def verify_task_commit(
    git: TaskGraphGit, task: dict[str, Any], attempt: dict[str, Any],
    commit: str, log_dir: Path,
) -> dict[str, Any]:
    """Return persisted evidence; agent messages are never proof of passing tests."""
    evidence: dict[str, Any] = {"passed": False, "commitSha": commit, "commands": [], "startedAt": time.time()}
    worktree = Path(attempt["worktree"])
    try:
        require_execution_contract(task)
        inspection = git.inspect_one_task_commit(worktree, attempt["launchBaseSha"])
        if not inspection.valid or inspection.commit_sha != commit:
            raise ValueError("worker no longer has exactly the expected task commit based on launchBaseSha")
        if not git.is_clean(worktree):
            raise ValueError("worker worktree has staged, unstaged, or untracked changes")
        paths = git.changed_paths(worktree, attempt["launchBaseSha"], commit)
        evidence["changedPaths"] = paths
        unexpected = [path for path in paths if (
            path == '.agent' or path.startswith('.agent/') or
            not any(path == allowed or (allowed.endswith('/') and path.startswith(allowed)) for allowed in task["predictedPaths"])
        )]
        if unexpected:
            raise ValueError("changes outside predictedPaths or in controller artifacts: " + ', '.join(unexpected))
        verification = task["verification"]
        if "skipReason" in verification:
            evidence["skipReason"] = verification["skipReason"]
        else:
            log_dir.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            env['PYTEST_ADDOPTS'] = (env.get('PYTEST_ADDOPTS', '') + ' -p no:cacheprovider').strip()
            for index, argv in enumerate(verification["commands"], 1):
                log = log_dir / f"command-{index}.log"
                result = {"argv": argv, "log": str(log), "startedAt": time.time(), "exitCode": None, "timedOut": False}
                evidence["commands"].append(result)
                with log.open('w', encoding='utf-8') as output:
                    try:
                        with subprocess.Popen(argv, cwd=worktree, env=env, stdin=subprocess.DEVNULL,
                                              stdout=output, stderr=subprocess.STDOUT, start_new_session=True) as process:
                            try:
                                result['exitCode'] = process.wait(timeout=verification.get('timeoutSeconds', 300))
                            except subprocess.TimeoutExpired:
                                result['timedOut'] = True
                                os.killpg(process.pid, signal.SIGKILL)
                                result['exitCode'] = process.wait()
                    except OSError as exc:
                        result['error'] = str(exc)
                        output.write(str(exc) + '\n')
                result['endedAt'] = time.time()
                if result['exitCode'] != 0 or result['timedOut']:
                    raise ValueError(f"verification command {index} failed; see {log}")
                if git.head_sha(worktree) != commit or not git.is_clean(worktree):
                    raise ValueError(f"verification command {index} changed HEAD or left a dirty worktree")
        evidence['passed'] = True
    except (DagValidationError, TaskGraphGitError, ValueError, OSError) as exc:
        evidence['reason'] = str(exc)
    evidence['endedAt'] = time.time()
    return evidence
