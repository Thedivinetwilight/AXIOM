"""Hot Path / Cold Path — separate compute paths, measure hot latency.

HOT: market update, feature, regime, event context, candidate scoring, net edge,
sizing, risk, execution — fast, memory-based.
COLD: downloads, evolution, optimization, MAE/MFE, calibration, alpha decay,
retirement evidence, event history — background workers.

Measure hot-path latency; do not allow research to block decision.
"""
from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Any
import logging

logger = logging.getLogger("bot.hot_path")


@dataclass
class HotPathMetrics:
    latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    max_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0
    p95_ms: float = 0.0
    count: int = 0
    cold_queue_depth: int = 0
    cold_completed: int = 0


class HotPathMonitor:
    def __init__(self, max_latency_ms: float = 100.0):
        self.max_latency = float(max_latency_ms)
        self.metrics = HotPathMetrics()
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings):
        return cls(max_latency_ms=float(getattr(settings, "hot_path_max_latency_ms", 100.0)))

    def record(self, latency_ms: float):
        with self._lock:
            self.metrics.latencies.append(float(latency_ms))
            self.metrics.count += 1
            vals = list(self.metrics.latencies)
            if vals:
                self.metrics.max_latency_ms = max(vals)
                self.metrics.avg_latency_ms = sum(vals) / len(vals)
                sorted_vals = sorted(vals)
                idx = int(len(sorted_vals) * 0.95)
                idx = min(idx, len(sorted_vals) - 1)
                self.metrics.p95_ms = sorted_vals[idx]
            if latency_ms > self.max_latency:
                logger.warning("HOT_PATH_LATENCY_HIGH", extra={"latency_ms": latency_ms, "threshold": self.max_latency})

    def time_block(self, func: Callable[[], Any]) -> tuple[Any, float]:
        start = time.perf_counter()
        result = func()
        latency = (time.perf_counter() - start) * 1000
        self.record(latency)
        return result, latency

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count": self.metrics.count,
                "max_latency_ms": round(self.metrics.max_latency_ms, 2),
                "avg_latency_ms": round(self.metrics.avg_latency_ms, 2),
                "p95_ms": round(self.metrics.p95_ms, 2),
                "threshold_ms": self.max_latency,
                "cold_queue_depth": self.metrics.cold_queue_depth,
                "cold_completed": self.metrics.cold_completed,
            }


class ColdPathQueue:
    def __init__(self):
        self._queue: deque = deque()
        self._lock = threading.Lock()
        self._completed = 0

    def submit(self, task: Callable[[], Any], name: str = "cold_task"):
        with self._lock:
            self._queue.append((name, task))
            # bounded: max 100
            if len(self._queue) > 100:
                self._queue.popleft()

    def run_one(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            name, task = self._queue.popleft()
        try:
            task()
            with self._lock:
                self._completed += 1
            return True
        except Exception as exc:
            logger.warning("COLD_TASK_FAILED", extra={"task": name, "error": type(exc).__name__})
            return False

    def run_all(self):
        while self.run_one():
            pass

    def depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def completed(self) -> int:
        with self._lock:
            return self._completed
