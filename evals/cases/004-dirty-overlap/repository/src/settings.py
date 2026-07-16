"""Settings validation."""


def is_valid_port(value: int) -> bool:
    """Return whether a TCP port value is valid."""
    return 1 <= value <= 65_535
