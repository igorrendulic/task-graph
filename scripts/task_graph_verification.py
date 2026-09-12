"""Controller-owned verification of the exact worker commit before integration."""

from __future__ import annotations

import fcntl
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


class VerificationJob:
    """Poll command completion without blocking the controller's event loop.

    The command inherits the attempt lock. If the controller dies, a replacement
    waits for that command to exit before rechecking the same worktree.
    """

    def __init__(self, git: TaskGraphGit, task: dict[str, Any], attempt: dict[str, Any],
                 commit: str, log_dir: Path) -> None:
        self.git, self.task, self.attempt = git, task, attempt
        self.commit, self.log_dir = commit, log_dir
        self.worktree = Path(attempt['worktree'])
        self.evidence: dict[str, Any] = {
            'passed': False, 'commitSha': commit, 'commands': [], 'startedAt': time.time(),
        }
        self.process: subprocess.Popen | None = None
        self.output = None
        self.lock = None
        self.waiting_for_lock = False
        self.initialized = False
        self.deadline = 0.0

    def _prepare(self) -> None:
        require_execution_contract(self.task)
        inspection = self.git.inspect_one_task_commit(self.worktree, self.attempt['launchBaseSha'])
        if not inspection.valid or inspection.commit_sha != self.commit:
            raise ValueError('worker no longer has exactly the expected task commit based on launchBaseSha')
        if not self.git.is_clean(self.worktree):
            raise ValueError('worker worktree has staged, unstaged, or untracked changes')
        paths = self.git.changed_paths(self.worktree, self.attempt['launchBaseSha'], self.commit)
        self.evidence['changedPaths'] = paths
        unexpected = [path for path in paths if (
            path == '.agent' or path.startswith('.agent/') or
            not any(path == allowed or (allowed.endswith('/') and path.startswith(allowed))
                    for allowed in self.task['predictedPaths'])
        )]
        if unexpected:
            raise ValueError('changes outside predictedPaths or in controller artifacts: ' + ', '.join(unexpected))

    def _check_worktree(self) -> None:
        if not self.git.is_clean(self.worktree):
            raise ValueError('worker worktree has staged, unstaged, or untracked changes')
        if self.git.head_sha(self.worktree) != self.commit:
            raise ValueError('verification changed HEAD')

    def poll(self) -> bool:
        """Return True only when final verification evidence is available."""
        if 'endedAt' in self.evidence:
            return True
        try:
            if not self.initialized:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                if self.lock is None:
                    self.lock = (self.log_dir / 'verification.lock').open('a')
                try:
                    fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.waiting_for_lock = True
                    return False
                self.waiting_for_lock = False
                self._prepare()
                self.initialized = True
                if 'skipReason' in self.task['verification']:
                    self.evidence['skipReason'] = self.task['verification']['skipReason']
                    return self._finish()
            if self.process is not None:
                result = self.evidence['commands'][-1]
                code = self.process.poll()
                if code is None:
                    if time.monotonic() < self.deadline:
                        return False
                    result['timedOut'] = True
                    self._stop_command()
                    code = self.process.returncode
                result.update(exitCode=code, endedAt=time.time())
                self.process = None
                self.output.close()
                self.output = None
                if code != 0 or result['timedOut']:
                    label = 'timed out' if result['timedOut'] else f'failed (exit {code})'
                    raise ValueError(f"verification {label}: {' '.join(result['argv'])}; see {result['log']}")
                self._check_worktree()
            commands = self.task['verification']['commands']
            index = len(self.evidence['commands'])
            if index == len(commands):
                return self._finish()
            argv = commands[index]
            log = self.log_dir / f'command-{index + 1}.log'
            result = {'argv': argv, 'log': str(log), 'startedAt': time.time(),
                      'exitCode': None, 'timedOut': False}
            self.evidence['commands'].append(result)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            env['PYTEST_ADDOPTS'] = (env.get('PYTEST_ADDOPTS', '') + ' -p no:cacheprovider').strip()
            self.output = log.open('w', encoding='utf-8')
            try:
                self.process = subprocess.Popen(
                    argv, cwd=self.worktree, env=env, stdin=subprocess.DEVNULL,
                    stdout=self.output, stderr=subprocess.STDOUT, start_new_session=True,
                    pass_fds=(self.lock.fileno(),),
                )
            except OSError as exc:
                result.update(error=str(exc), endedAt=time.time())
                raise ValueError(f"cannot start verification: {' '.join(argv)}; {exc}") from exc
            self.deadline = time.monotonic() + self.task['verification'].get('timeoutSeconds', 300)
            return False
        except (DagValidationError, TaskGraphGitError, ValueError, OSError) as exc:
            return self._finish(str(exc))

    def _finish(self, reason: str | None = None) -> bool:
        self.evidence.update(passed=reason is None, endedAt=time.time())
        if reason is not None:
            self.evidence['reason'] = reason
        self._release()
        return True

    def _stop_command(self) -> None:
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()

    def _release(self) -> None:
        if self.output is not None:
            self.output.close()
            self.output = None
        if self.lock is not None:
            # Close, rather than LOCK_UN: an orphaned command may own this lock too.
            self.lock.close()
            self.lock = None

    def close(self) -> None:
        if 'endedAt' not in self.evidence:
            self._stop_command()
            if self.process is not None:
                self.evidence['commands'][-1].update(exitCode=self.process.returncode, endedAt=time.time())
                self.process = None
            self._finish('verification interrupted by controller shutdown')
        self._release()


def verify_task_commit(
    git: TaskGraphGit, task: dict[str, Any], attempt: dict[str, Any],
    commit: str, log_dir: Path,
) -> dict[str, Any]:
    """Synchronous entry point for callers that do not run a controller loop."""
    job = VerificationJob(git, task, attempt, commit, log_dir)
    try:
        while not job.poll():
            time.sleep(.02)
        return job.evidence
    finally:
        job.close()
