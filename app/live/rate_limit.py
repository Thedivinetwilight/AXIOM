"""Broker API rate-limit policy (master spec section 40).

Upstox's published limits (at spec-authoring time) for combined order APIs:

* regular algo:   10 requests/sec, 500/min, 2000/30min;
* SEBI-registered algo: 50 requests/sec, 500/min, 2000/30min;
* other standard APIs:  50/sec, 500/min, 2000/30min.

These are NOT encoded as eternal constants: the policy is data, configurable
per broker profile, and the limiter exposes its effective limits for
diagnostics. The limiter provides request accounting, admission decisions,
throttle-until timestamps and bounded rejection semantics. It never places an
order itself, and a rejected admission must never be retried into a duplicate
submission — callers keep the deterministic intent id so a retry reuses it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


DEFAULT_PROFILES: dict[str, dict[str, int]] = {
    "REGULAR_ALGO": {"per_sec": 10, "per_min": 500, "per_30min": 2000},
    "REGISTERED_ALGO": {"per_sec": 50, "per_min": 500, "per_30min": 2000},
    "STANDARD": {"per_sec": 50, "per_min": 500, "per_30min": 2000},
}


@dataclass
class Admission:
    allowed: bool
    retry_after_s: float
    reason: str
    counts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "retry_after_s": round(self.retry_after_s, 3),
                "reason": self.reason, "counts": self.counts}


class RateLimiter:
    """Sliding-window limiter for one broker profile."""

    def __init__(self, profile: str = "REGULAR_ALGO", *, clock=time.monotonic):
        profile = profile.upper()
        if profile not in DEFAULT_PROFILES:
            raise KeyError(f"unknown rate-limit profile '{profile}'")
        self.profile = profile
        self.limits = dict(DEFAULT_PROFILES[profile])
        self._clock = clock
        self._events: deque[float] = deque()
        self._rejected = 0
        self._admitted = 0

    def configure(self, *, per_sec: int | None = None, per_min: int | None = None,
                  per_30min: int | None = None) -> dict:
        if per_sec is not None:
            self._validate_bound("per_sec", per_sec)
            self.limits["per_sec"] = per_sec
        if per_min is not None:
            self._validate_bound("per_min", per_min)
            self.limits["per_min"] = per_min
        if per_30min is not None:
            self._validate_bound("per_30min", per_30min)
            self.limits["per_30min"] = per_30min
        return dict(self.limits)

    @staticmethod
    def _validate_bound(name: str, value: int) -> None:
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer, got {value!r}")

    def _prune(self, now: float) -> None:
        window_start = now - 1800.0
        while self._events and self._events[0] < window_start:
            self._events.popleft()

    def _count_within(self, now: float, seconds: float) -> int:
        cutoff = now - seconds
        return sum(1 for ts in self._events if ts >= cutoff)

    def counts(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        self._prune(now)
        return {
            "per_sec": self._count_within(now, 1.0),
            "per_min": self._count_within(now, 60.0),
            "per_30min": self._count_within(now, 1800.0),
        }

    def admit(self, *, now: float | None = None) -> Admission:
        """Decide whether ONE request may proceed right now.

        Admitting records the event; rejected admissions record nothing. The
        returned ``retry_after_s`` is the minimum wait for the tightest bound.
        """
        now = self._clock() if now is None else now
        self._prune(now)
        current = self.counts(now)
        waits = []
        if current["per_sec"] >= self.limits["per_sec"]:
            waits.append(self._oldest_wait(now, 1.0))
        if current["per_min"] >= self.limits["per_min"]:
            waits.append(self._oldest_wait(now, 60.0))
        if current["per_30min"] >= self.limits["per_30min"]:
            waits.append(self._oldest_wait(now, 1800.0))
        if waits:
            self._rejected += 1
            wait = max(waits)
            return Admission(False, wait,
                             "rate limit reached; retry after the computed window",
                             counts=current)
        self._events.append(now)
        self._admitted += 1
        return Admission(True, 0.0, "within limits", counts=self.counts(now))

    def _oldest_wait(self, now: float, window: float) -> float:
        cutoff = now - window
        for ts in self._events:
            if ts >= cutoff:
                return max(0.0, ts + window - now)
        return 0.0

    def stats(self) -> dict:
        return {"profile": self.profile, "limits": dict(self.limits),
                "admitted": self._admitted, "rejected": self._rejected}
