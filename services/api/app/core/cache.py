from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Generic, Optional, TypeVar


T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    expires_at: float
    value: T


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, CacheEntry[T]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Optional[T]:
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._store.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value: T, ttl_seconds: Optional[int] = None) -> T:
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        with self._lock:
            self._store[key] = CacheEntry(expires_at=time.time() + ttl, value=value)
        return value

    def clear(self):
        with self._lock:
            self._store.clear()
