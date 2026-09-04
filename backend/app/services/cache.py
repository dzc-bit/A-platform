from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from ..config import settings

try:  # Redis is optional; the deterministic local cache remains the default.
    import redis  # type: ignore
except ImportError:  # pragma: no cover - depends on optional deployment package
    redis = None  # type: ignore


@dataclass(frozen=True)
class CacheStatus:
    mode: str
    hits: int
    misses: int


class RetrievalCache:
    """Best-effort retrieval cache with a bounded in-process fallback.

    Redis is deliberately optional: a missing or temporarily unavailable Redis server
    must not turn a knowledge lookup into an application failure.
    """

    def __init__(self, max_entries: int = 512) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._memory: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._redis_client: Any | None = None
        self._redis_retry_after = 0.0
        self._redis_keys: set[str] = set()
        self._hits = 0
        self._misses = 0

    def _mark_redis_unavailable(self) -> None:
        # Lock-free by design: these fields may be written from any thread and
        # can run network I/O that must not happen while holding ``self._lock``.
        # The GIL keeps the assignments atomic and every write is idempotent;
        # the worst interleaving recreates the lazy Redis client once.
        self._redis_client = None
        # Keep Redis connection failures off the request hot path, but retry later so
        # a restarted cache server can recover without restarting the API process.
        self._redis_retry_after = time.monotonic() + 30

    def _client(self) -> Any | None:
        if not settings.redis_url or redis is None:
            return None
        if time.monotonic() < self._redis_retry_after:
            return None
        try:
            if self._redis_client is None:
                self._redis_client = redis.Redis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=0.25,
                    socket_timeout=0.25,
                )
                self._redis_client.ping()
            return self._redis_client
        except Exception:  # Network cache must never block an answer.
            self._mark_redis_unavailable()
            return None

    def _record_hit(self) -> None:
        with self._lock:
            self._hits += 1

    def _record_miss(self) -> None:
        with self._lock:
            self._misses += 1

    def _prune_memory_locked(self, now: float) -> None:
        """Evict expired then oldest entries. Caller must hold ``self._lock``."""
        expired = [key for key, (expires_at, _) in self._memory.items() if expires_at <= now]
        for key in expired:
            self._memory.pop(key, None)
        while len(self._memory) > self._max_entries:
            oldest_key = min(self._memory, key=lambda key: self._memory[key][0])
            self._memory.pop(oldest_key, None)

    def get(self, key: str) -> str | None:
        client = self._client()
        if client is not None:
            try:
                value = client.get(key)
                if value is not None:
                    self._record_hit()
                    return str(value)
            except Exception:
                self._mark_redis_unavailable()
        now = time.monotonic()
        with self._lock:
            item = self._memory.get(key)
            if item is not None and item[0] > now:
                self._hits += 1
                return item[1]
            self._prune_memory_locked(now)
        self._record_miss()
        return None

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        client = self._client()
        if client is not None:
            try:
                client.setex(key, ttl_seconds, value)
                with self._lock:
                    self._redis_keys.add(key)
                return
            except Exception:
                self._mark_redis_unavailable()
        with self._lock:
            self._memory[key] = (time.monotonic() + ttl_seconds, value)
            self._prune_memory_locked(time.monotonic())

    def status(self) -> CacheStatus:
        if self._client() is not None:
            mode = "redis"
        elif settings.redis_url:
            mode = "memory_fallback"
        else:
            mode = "memory"
        with self._lock:
            return CacheStatus(mode=mode, hits=self._hits, misses=self._misses)

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()
            redis_keys = tuple(self._redis_keys)
            self._redis_keys.clear()
            # The hit/miss counters are incremented under this lock in get(),
            # _record_hit() and _record_miss(); resetting them here too keeps a
            # concurrent clear() from clobbering (losing) a parallel update.
            self._hits = 0
            self._misses = 0
        client = self._client()
        if client is not None and redis_keys:
            try:
                client.delete(*redis_keys)
            except Exception:
                self._mark_redis_unavailable()


retrieval_cache = RetrievalCache()
