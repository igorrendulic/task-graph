"""Best-effort platform notifications for completed Task Graph runs."""

from __future__ import annotations

import json
import platform
import re
import subprocess


def notify_completion(*, succeeded: bool, message: str) -> dict[str, str]:
    """Attempt a macOS notification and return its safe, observable outcome."""
    if platform.system() != "Darwin":
        return {"outcome": "unsupported"}
    title = "Task Graph succeeded" if succeeded else "Task Graph failed"
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        result = subprocess.run(
            ["osascript", "-e", script], check=False, capture_output=True, text=True
        )
    except OSError as exc:
        return {"outcome": "failed", "error": f"osascript unavailable: {_sanitize_error(str(exc))}"}
    if result.returncode == 0:
        return {"outcome": "delivered"}
    detail = _sanitize_error(result.stderr or "")
    error = f"osascript exited {result.returncode}"
    if detail:
        error = f"{error}: {detail}"
    return {"outcome": "failed", "error": error}


def _sanitize_error(value: str) -> str:
    """Keep OS errors safe to display in terminal status output."""
    without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", " ", value)
    without_controls = "".join(
        character for character in without_ansi if character >= " " or character == "\t"
    )
    return " ".join(without_controls.split())[:500]
