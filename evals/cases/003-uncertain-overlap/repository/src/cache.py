"""Cache invalidation facade with an intentionally abstract integration boundary."""

from typing import Protocol


class CacheBackend(Protocol):
    """Storage contract implemented by an integration outside this fixture."""

    def get(self, key: str) -> str | None:
        """Return a cached value when present."""

    def delete(self, key: str) -> bool:
        """Delete a cached value and report whether it existed."""


class MemoryCache:
    """In-memory backend used to exercise the invalidation facade."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def delete(self, key: str) -> bool:
        return self._values.pop(key, None) is not None


class CacheInvalidator:
    """Coordinates invalidation without selecting a concrete integration."""

    def __init__(self, backend: CacheBackend) -> None:
        self._backend = backend

    def invalidate(self, key: str) -> bool:
        """Invalidate one key through the configured backend."""
        return self._backend.delete(key)
