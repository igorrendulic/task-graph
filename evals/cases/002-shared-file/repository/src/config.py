"""Configuration model and loading boundary for the service."""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ServiceConfig:
    """Normalized configuration consumed by the running service."""

    host: str
    port: int
    use_tls: bool


def load_config(values: Mapping[str, str]) -> ServiceConfig:
    """Load the configuration values that parser and serializer work share."""
    host = values.get("HOST", "127.0.0.1")
    port = int(values.get("PORT", "8000"))
    use_tls = values.get("USE_TLS", "false").lower() == "true"
    return ServiceConfig(host=host, port=port, use_tls=use_tls)
