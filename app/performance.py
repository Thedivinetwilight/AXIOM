"""Bounded, payload-free latency observations for operator diagnostics.

Timing is diagnostic only. It must never affect an order decision, and no
request body, credential, account identifier or symbol is accepted here.
"""

from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from math import isfinite
from threading import RLock
from time import perf_counter


class LatencyTracker:
    """Keep at most ``capacity`` newest samples per named measurement."""

    def __init__(self, capacity: int = 512):
        if not 16 <= int(capacity) <= 4096:
            raise ValueError("Latency sample capacity must be between 16 and 4096")
        self.capacity = int(capacity)
        self.max_metrics = 64
        self._samples = defaultdict(lambda: deque(maxlen=self.capacity))
        self._counts = defaultdict(int)
        self._lock = RLock()

    def observe(self, name: str, milliseconds: float) -> None:
        """Record a finite duration; invalid diagnostics are ignored safely."""
        try:
            value = float(milliseconds)
        except (TypeError, ValueError, OverflowError):
            return
        if not isfinite(value) or value < 0 or not name:
            return
        # A bounded, known metric name prevents unbounded caller-created keys.
        if len(name) > 80:
            return
        with self._lock:
            if name not in self._counts and len(self._counts) >= self.max_metrics:
                return
            self._samples[name].append(value)
            self._counts[name] += 1

    @contextmanager
    def measure(self, name: str):
        started = perf_counter()
        try:
            yield
        finally:
            self.observe(name, (perf_counter() - started) * 1000.0)

    def clear(self) -> None:
        """Clear local observations (used by isolated benchmarks, not an API)."""
        with self._lock:
            self._samples.clear()
            self._counts.clear()

    @staticmethod
    def _percentile(ordered: list[float], quantile: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * quantile
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    def summary(self) -> dict:
        with self._lock:
            names = set(self._counts)
            snapshots = {name: list(self._samples[name]) for name in names}
            counts = dict(self._counts)
        metrics = {}
        for name in sorted(names):
            values = snapshots[name]
            ordered = sorted(values)
            metrics[name] = {
                "samples": counts[name],
                "window_samples": len(values),
                "p50_ms": round(self._percentile(ordered, 0.50), 3),
                "p95_ms": round(self._percentile(ordered, 0.95), 3),
                "p99_ms": round(self._percentile(ordered, 0.99), 3),
                "max_ms": round(ordered[-1], 3),
                "last_ms": round(values[-1], 3),
            }
        return {
            "unit": "milliseconds",
            "sample_capacity_per_metric": self.capacity,
            "max_metric_names": self.max_metrics,
            "metrics": metrics,
            "note": "Observed local-process timing only; no universal or zero-latency claim.",
        }
