"""Bounded caches — clear key, TTL/invalidation, max size, deterministic, restart behavior."""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class CacheEntry:
    value: any
    ts: float


class BoundedCache:
    def __init__(self, max_size: int = 200, ttl: float = 300, name: str = "cache"):
        self.max_size = int(max_size)
        self.ttl = float(ttl)
        self.name = str(name)
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, key: str):
        entry = self._store.get(key)
        if not entry:
            return None
        if time.time() - entry.ts > self.ttl:
            # expired
            self._store.pop(key, None)
            return None
        # move to end (LRU)
        self._store.move_to_end(key)
        return entry.value

    def set(self, key: str, value):
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = CacheEntry(value, time.time())
        # evict oldest if over size
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)

    def invalidate(self, key: str | None = None):
        if key:
            self._store.pop(key, None)
        else:
            self._store.clear()

    def __contains__(self, key: str):
        return self.get(key) is not None

    def __len__(self):
        # purge expired first
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.ts > self.ttl]
        for k in expired:
            self._store.pop(k, None)
        return len(self._store)

    def keys(self):
        # return non-expired keys
        now = time.time()
        return [k for k, v in self._store.items() if now - v.ts <= self.ttl]

    def stats(self) -> dict:
        return {"size": len(self), "max_size": self.max_size, "ttl": self.ttl, "name": self.name}
