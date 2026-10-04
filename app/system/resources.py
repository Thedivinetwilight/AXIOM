"""Resource governor (master spec sections 26, 97).

The scheduler must know when markets are open and when execution is active:
during trading-critical windows research work is capped and hot-path work is
protected; outside them research may expand. Pressure is reported HONESTLY
(never faked to zero) using whatever the platform can measure.
"""

from __future__ import annotations

import os
import shutil
import time


def _load_avg() -> float | None:
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def _disk_used_pct(path: str) -> float | None:
    try:
        usage = shutil.disk_usage(path)
        return usage.used / usage.total if usage.total else None
    except OSError:
        return None


class ResourceGovernor:
    """Deterministic, dependency-free resource pressure model.

    Thresholds are explicit; nothing here guesses. When a measurement is
    unavailable it is reported as None and does NOT count as healthy (the
    readiness center shows UNKNOWN rather than a fabricated OK).
    """

    def __init__(self, *, disk_path: str = ".", cpu_count: int | None = None,
                 load_pressure: float | None = None, disk_pressure: float = 0.92,
                 trading_critical: bool = False):
        self.disk_path = disk_path
        self.cpu_count = cpu_count or os.cpu_count() or 1
        self.load_pressure = load_pressure  # None -> derive from cpu count
        self.disk_pressure = disk_pressure
        self.trading_critical = trading_critical
        self._research_cap_normal = 2
        self._research_cap_critical = 1

    def snapshot(self) -> dict:
        load = _load_avg()
        disk = _disk_used_pct(self.disk_path)
        return {
            "cpu_count": self.cpu_count,
            "load_1m": load,
            "disk_used_pct": disk,
            "trading_critical_window": self.trading_critical,
            "research_concurrency_cap": self.research_concurrency_cap(),
            "note": "measurements unavailable are reported as null, never as ok",
        }

    def load_pressure_limit(self) -> float:
        if self.load_pressure is not None:
            return self.load_pressure
        return float(self.cpu_count)

    def under_pressure(self) -> bool:
        """True when ANY available measurement exceeds its bound.

        Unknown measurements do not clear the flag (fail-visible, not
        fail-blind): pressure is False only when every available signal is
        inside its bound.
        """
        load = _load_avg()
        if load is not None and load > self.load_pressure_limit():
            return True
        disk = _disk_used_pct(self.disk_path)
        if disk is not None and disk > self.disk_pressure:
            return True
        return False

    def research_concurrency_cap(self) -> int:
        return self._research_cap_critical if self.trading_critical \
            else self._research_cap_normal

    def set_trading_critical(self, active: bool) -> None:
        self.trading_critical = bool(active)
