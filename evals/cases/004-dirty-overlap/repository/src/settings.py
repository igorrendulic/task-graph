"""Service settings and validation logic."""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings."""

    port: int
    debug: bool


def validate_port(value: str) -> int:
    """Parse and validate a TCP port supplied by configuration."""
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    return port


def load_settings(values: Mapping[str, str]) -> Settings:
    """Load validated settings from string configuration values."""
    return Settings(
        port=validate_port(values.get("PORT", "8000")),
        debug=values.get("DEBUG", "false").lower() == "true",
    )
