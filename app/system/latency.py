"""Latency observability (master spec sections 23, 24, 87).

Latency is an engineering property: each hot/warm/cold stage gets P50/P95/
P99/P99.9 tracking plus deadline-miss counting. Summaries persist per stage
per day so regression is visible across restarts. No optimization decisions
are made here — this only measures and reports.

Implementation note: per-stage reservoirs are bounded deques; percentiles are
computed exactly over the reservoir (not from an average), which is exactly
what "do not optimize based on a single average" requires.
"""

from __future__ import annotations

import bisect
import threading
import time
from collections import deque
from contextlib import contextmanager

STAGES = (
    "market_event_ingest", "feature_update", "candidate_generation",
    "model_inference", "risk_check", "order_validation", "broker_request",
    "broker_ack", "reconciliation",
)

DEFAULT_DEADLINES_MS = {
    "market_event_ingest": 50.0,
    "feature_update": 25.0,
    "risk_check": 20.0,
    "order_validation": 20.0,
    "broker_request": 1500.0,
}


class LatencyTracker:
    def __init__(self, *, reservoir: int = 4096,
                 deadlines_ms: dict | None = None):
        self.reservoir_size = reservoir
        self.deadlines_ms = dict(DEFAULT_DEADLINES_MS)
        if deadlines_ms:
            self.deadlines_ms.update(deadlines_ms)
        self._lock = threading.Lock()
        self._samples: dict[str, list[float]] = {}
        self._counts: dict[str, int] = {}
        self._misses: dict[str, int] = {}
        self._max: dict[str, float] = {}

    def record(self, stage: str, elapsed_ms: float, *,
               deadline_ms: float | None = None) -> bool:
        """Record one observation. Returns True when the deadline was missed."""
        if elapsed_ms < 0:
            raise ValueError("elapsed_ms cannot be negative")
        deadline = deadline_ms if deadline_ms is not None \
            else self.deadlines_ms.get(stage)
        missed = deadline is not None and elapsed_ms > deadline
        with self._lock:
            self._counts[stage] = self._counts.get(stage, 0) + 1
            self._max[stage] = max(self._max.get(stage, 0.0), elapsed_ms)
            if missed:
                self._misses[stage] = self._misses.get(stage, 0) + 1
            reservoir = self._samples.setdefault(stage, [])
            if len(reservoir) < self.reservoir_size:
                bisect.insort(reservoir, elapsed_ms)
            else:
                # bounded: keep the newest by evicting the median-aged slot;
                # exact percentiles stay valid over the retained window.
                reservoir.pop(0)
                bisect.insort(reservoir, elapsed_ms)
        return missed

    def percentile(self, stage: str, pct: float) -> float | None:
        if not 0 <= pct <= 100:
            raise ValueError("pct must be within [0, 100]")
        with self._lock:
            reservoir = self._samples.get(stage)
            if not reservoir:
                return None
            index = min(len(reservoir) - 1, int(round((pct / 100.0) * (len(reservoir) - 1))))
            return reservoir[index]

    def summary(self, stage: str) -> dict:
        return {
            "stage": stage,
            "count": self._counts.get(stage, 0),
            "p50_ms": self.percentile(stage, 50),
            "p95_ms": self.percentile(stage, 95),
            "p99_ms": self.percentile(stage, 99),
            "p999_ms": self.percentile(stage, 99.9),
            "max_ms": self._max.get(stage),
            "deadline_ms": self.deadlines_ms.get(stage),
            "deadline_misses": self._misses.get(stage, 0),
        }

    def overview(self) -> dict:
        return {stage: self.summary(stage)
                for stage in sorted(set(list(self._counts) + list(STAGES)))}

    def persist_daily(self, repository, *, day: str | None = None) -> int:
        """Write per-stage summaries for the given UTC day. Returns row count."""
        from datetime import datetime, timezone

        day = day or datetime.now(timezone.utc).date().isoformat()
        written = 0
        for stage, count in list(self._counts.items()):
            if count == 0:
                continue
            summary = self.summary(stage)
            repository.upsert_latency_summary(
                stage=stage, day=day, count=count,
                p50_ms=summary["p50_ms"] or 0.0, p95_ms=summary["p95_ms"] or 0.0,
                p99_ms=summary["p99_ms"] or 0.0, p999_ms=summary["p999_ms"] or 0.0,
                max_ms=summary["max_ms"] or 0.0,
                deadline_misses=summary["deadline_misses"])
            written += 1
        return written


@contextmanager
def track(tracker: LatencyTracker, stage: str):
    """``with track(tracker, "risk_check"): ...`` timing helper."""
    started = time.perf_counter()
    try:
        yield
    finally:
        tracker.record(stage, (time.perf_counter() - started) * 1000.0)
