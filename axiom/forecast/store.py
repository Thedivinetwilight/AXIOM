"""ForecastStore — the forecast-side store, separate from any Arena store.

It holds validated ``ForecastSnapshot`` objects keyed by ``(instrument,
timeframe)`` plus the cache keys that produced them, so the pipeline can reuse a
forecast when the closed-candle window has not changed. It imports nothing from
Arena and is guarded by a lock because the Kronos worker thread writes while the
GUI thread reads.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import datetime, timedelta

from axiom.models import ForecastSnapshot, KronosState, utcnow

Key = tuple[str, str]


class ForecastStore:
    """Thread-safe latest-forecast store with cache-key bookkeeping."""

    def __init__(self, history_limit: int = 32) -> None:
        self._lock = threading.RLock()
        self._latest: dict[Key, ForecastSnapshot] = {}
        self._history: dict[Key, list[ForecastSnapshot]] = {}
        self._cache_keys: dict[Key, str] = {}
        self._history_limit = max(1, history_limit)
        self._hits = 0
        self._misses = 0

    # -- writes --------------------------------------------------------------
    def put(self, snapshot: ForecastSnapshot) -> ForecastSnapshot:
        key = (snapshot.instrument, snapshot.timeframe)
        with self._lock:
            self._latest[key] = snapshot
            history = self._history.setdefault(key, [])
            history.append(snapshot)
            del history[: max(0, len(history) - self._history_limit)]
            self._cache_keys[key] = snapshot.cache_key
        return snapshot

    def register_cache_key(self, instrument: str, timeframe: str, cache_key: str) -> None:
        with self._lock:
            self._cache_keys[(instrument, timeframe)] = cache_key

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
            self._history.clear()
            self._cache_keys.clear()

    # -- reads ---------------------------------------------------------------
    def latest(self, instrument: str, timeframe: str) -> ForecastSnapshot | None:
        with self._lock:
            return self._latest.get((instrument, timeframe))

    def all_latest(self) -> list[ForecastSnapshot]:
        with self._lock:
            return sorted(
                self._latest.values(),
                key=lambda s: (s.instrument, s.timeframe),
            )

    def history(self, instrument: str, timeframe: str) -> list[ForecastSnapshot]:
        with self._lock:
            return list(self._history.get((instrument, timeframe), ()))

    def keys(self) -> list[Key]:
        with self._lock:
            return sorted(self._latest)

    def cache_key(self, instrument: str, timeframe: str) -> str | None:
        with self._lock:
            return self._cache_keys.get((instrument, timeframe))

    def has_cache_key(self, instrument: str, timeframe: str, cache_key: str) -> bool:
        with self._lock:
            return self._cache_keys.get((instrument, timeframe)) == cache_key

    def take_cache_hit(self) -> ForecastSnapshot | None:
        """Record a cache hit against the current key (statistics only)."""
        with self._lock:
            self._hits += 1
        return None

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "series": len(self._latest),
                "cache_hits": self._hits,
                "cache_misses": self._misses,
            }

    def record_miss(self) -> None:
        with self._lock:
            self._misses += 1

    # -- staleness -----------------------------------------------------------
    def stale_series(
        self, now: datetime | None = None, stale_after: timedelta | float = 3600.0
    ) -> list[Key]:
        """Series whose newest forecast is older than ``stale_after``."""
        moment = now or utcnow()
        limit = (
            stale_after
            if isinstance(stale_after, timedelta)
            else timedelta(seconds=float(stale_after))
        )
        with self._lock:
            return [
                key
                for key, snapshot in sorted(self._latest.items())
                if moment - snapshot.generated_at > limit
            ]

    def state_for(
        self,
        instrument: str,
        timeframe: str,
        runtime_state: KronosState,
        *,
        now: datetime | None = None,
        stale_after: float = 3600.0,
    ) -> KronosState:
        """Effective UI state for a series (STALE wins over a healthy runtime)."""
        snapshot = self.latest(instrument, timeframe)
        if snapshot is None:
            return runtime_state
        moment = now or utcnow()
        age = (moment - snapshot.generated_at).total_seconds()
        if age > stale_after and runtime_state in {KronosState.READY, KronosState.INFERENCING}:
            return KronosState.STALE
        return runtime_state

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> dict:
        with self._lock:
            return {
                "series": [snapshot.to_dict() for snapshot in self.all_latest()],
                "stats": dict(self.stats),
            }

    def export_series(self, series: Iterable[ForecastSnapshot]) -> list[dict]:
        return [snapshot.to_dict() for snapshot in series]
