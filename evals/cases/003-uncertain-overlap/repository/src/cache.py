"""Cache boundary."""


def invalidate_key(key: str) -> str:
    """Return the invalidation event for a cache key."""
    return f"invalidate:{key}"
