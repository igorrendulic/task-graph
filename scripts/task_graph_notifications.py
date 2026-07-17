"""Best-effort platform notifications for completed Task Graph runs."""

from __future__ import annotations

import json
import platform
import subprocess


def notify_completion(*, succeeded: bool, message: str) -> None:
    """Show a macOS notification without affecting the controller's outcome."""
    if platform.system() != "Darwin":
        return
    title = "Task Graph succeeded" if succeeded else "Task Graph failed"
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True, text=True)
    except OSError:
        return
