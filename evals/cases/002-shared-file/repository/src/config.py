"""Configuration helpers."""


def parse_assignment(text: str) -> tuple[str, str]:
    """Split a simple key-value assignment."""
    key, value = text.split("=", maxsplit=1)
    return key.strip(), value.strip()
