"""In-process idempotency cache.

Caches full JSON response bodies keyed by X-TC-Idempotency for 24 hours.
On a duplicate key within the window, returns the cached response without
calling the broker again.

Upgrade path to Redis
---------------------
Replace IdempotencyCache.get / .set with calls to a Redis client:
  redis.get(key) -> bytes | None
  redis.setex(key, 86400, json_bytes)
No other code needs to change.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from cachetools import TTLCache

logger = logging.getLogger(__name__)

_TTL_SECONDS = 86_400  # 24 hours
_MAX_KEYS = 50_000      # cap memory usage


class IdempotencyCache:
    """Thread-safe (GIL-protected) TTL LRU cache."""

    def __init__(self) -> None:
        self._cache: TTLCache = TTLCache(maxsize=_MAX_KEYS, ttl=_TTL_SECONDS)

    def get(self, key: str) -> dict[str, Any] | None:
        return self._cache.get(key)

    def set(self, key: str, response: dict[str, Any]) -> None:
        self._cache[key] = response

    def __contains__(self, key: str) -> bool:
        return key in self._cache


# Module-level singleton
_cache = IdempotencyCache()


def get_idempotency_cache() -> IdempotencyCache:
    """FastAPI dependency — returns the singleton cache."""
    return _cache
