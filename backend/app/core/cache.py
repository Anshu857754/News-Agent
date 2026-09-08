"""A small in-memory TTL cache.

Trend feeds and LLM classifications are the two expensive things this service
does, and both are stable over minutes. Caching them keeps the provider's
goodwill and the OpenRouter bill in check.

Deliberately not Redis: the project has no Redis, and an MVP that needs a
second process to boot is a worse MVP. The trade-off is explicit - this cache
is per-process, so it empties on restart and is not shared between workers.
That is acceptable for data with a 15-minute TTL.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# A guard, not a tuning knob: without it a long-running process could hold one
# entry per region/limit combination ever requested.
DEFAULT_MAX_ENTRIES = 256


class TTLCache:
    """Thread-safe key/value store where every entry expires.

    Uvicorn runs sync endpoints in a threadpool, so the lock is not optional
    even though the async path is single-threaded.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.ttl = max(0.0, float(ttl_seconds))
        self.max_entries = max_entries
        # Injectable so tests can expire entries without sleeping.
        self._clock = clock or time.monotonic
        self._entries: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """A zero TTL disables the cache without changing any call site."""
        return self.ttl > 0

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value, or None when missing or expired."""
        if not self.enabled:
            return None
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._entries[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        now = self._clock()
        with self._lock:
            if len(self._entries) >= self.max_entries:
                self._evict_expired(now)
            if len(self._entries) >= self.max_entries:
                # Still full: drop whatever expires soonest.
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
            self._entries[key] = (now + self.ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _evict_expired(self, now: float) -> None:
        """Caller must hold the lock."""
        dead = [k for k, (expires_at, _) in self._entries.items() if expires_at <= now]
        for key in dead:
            del self._entries[key]
        if dead:
            log.debug("cache: evicted %d expired entries", len(dead))
