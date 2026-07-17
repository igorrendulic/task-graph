"""Tmux session and pane operations for the Task Graph controller."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class TmuxError(RuntimeError):
    """Raised when tmux cannot create or inspect a controller resource."""


@dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    pid: int


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class TmuxClient:
    """A minimal tmux adapter; callers own all scheduling decisions."""

    def __init__(self, *, runner: Runner | None = None) -> None:
        self._runner = runner or self._default_runner

    def create_session(self, session: str, cwd: Path, command: str) -> str:
        return self._create(
            [
                "tmux",
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-s",
                session,
                "-n",
                "controller",
                "-c",
                str(cwd),
                command,
            ]
        )

    def create_window(self, session: str, name: str, cwd: Path, command: str) -> str:
        return self._create(
            [
                "tmux",
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                session,
                "-n",
                name,
                "-c",
                str(cwd),
                command,
            ]
        )

    def pane_info(self, pane_id: str) -> PaneInfo | None:
        result = self._runner(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_id} #{pane_pid}"]
        )
        if result.returncode != 0:
            return None
        fields = result.stdout.strip().split()
        if len(fields) != 2 or not fields[1].isdigit():
            return None
        return PaneInfo(pane_id=fields[0], pid=int(fields[1]))

    def session_exists(self, session: str) -> bool:
        return self._runner(["tmux", "has-session", "-t", session]).returncode == 0

    def pane_is_live(self, pane_id: str, expected_pid: int) -> bool:
        info = self.pane_info(pane_id)
        if info is None or info.pid != expected_pid:
            return False
        try:
            os.kill(expected_pid, 0)
        except OSError:
            return False
        return True

    def _create(self, command: list[str]) -> str:
        result = self._runner(command)
        if result.returncode != 0 or not result.stdout.strip():
            details = result.stderr.strip() or "tmux did not return a pane ID"
            raise TmuxError(details)
        return result.stdout.strip()

    @staticmethod
    def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)
