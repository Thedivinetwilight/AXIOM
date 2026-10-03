"""SystemMonitor: host + database health snapshot, persisted and alerting.

Records a system_status row (and a SYSTEM_ALERT event when a threshold is
crossed): load, RSS, memory %, disk %, database size + quick_check result.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ALERT_THRESHOLDS = {"disk_used_pct": 0.90, "memory_used_pct": 0.92}

# `resource` is POSIX-only. Importing it unguarded made this whole module
# unimportable on Windows, which broke `python -m app monitor` and every
# system snapshot taken by the engine there.
try:  # pragma: no cover - platform dependent
    import resource as _resource
except ImportError:  # Windows
    _resource = None  # type: ignore[assignment]


def _resident_mb() -> float | None:
    """Resident set size in MB, or None where the platform cannot report it."""
    if _resource is not None:
        # ru_maxrss is KiB on Linux, bytes on macOS.
        raw = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return round(raw / divisor, 1)
    if os.name == "nt":  # pragma: no cover - Windows only
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), ctypes.sizeof(_Counters)):
                return round(counters.WorkingSetSize / (1024 * 1024), 1)
        except (AttributeError, OSError, ValueError):
            return None
    return None


def _memory_used_pct() -> float | None:
    try:
        with open("/proc/meminfo") as handle:
            info = {}
            for line in handle:
                key, _, rest = line.partition(":")
                info[key.strip()] = float(rest.split()[0])
        total = info.get("MemTotal", 0.0)
        available = info.get("MemAvailable", 0.0)
        return 1.0 - available / total if total > 0 else None
    except (OSError, ValueError, KeyError):
        return None


class SystemMonitor:
    def __init__(self, repository, *, data_dir: str = "data",
                 database_path: str | None = None):
        self.repository = repository
        self.data_dir = Path(data_dir)
        self.database_path = database_path

    def snapshot(self) -> dict:
        payload: dict = {"ts": datetime.now(timezone.utc).isoformat()}
        try:
            loads = os.getloadavg()
            payload["load_1m"] = round(loads[0], 3)
        except (AttributeError, OSError):
            payload["load_1m"] = None
        payload["rss_mb"] = _resident_mb()
        memory_used = _memory_used_pct()
        if memory_used is not None:
            payload["memory_used_pct"] = round(memory_used, 4)
        try:
            usage = shutil.disk_usage(self.data_dir)
            payload["disk_used_pct"] = round(usage.used / usage.total, 4)
        except OSError:
            payload["disk_used_pct"] = None
        if self.database_path and Path(self.database_path).exists():
            payload["database_mb"] = round(
                Path(self.database_path).stat().st_size / (1024 * 1024), 2)
            try:
                with sqlite3.connect(self.database_path.replace("sqlite:///", "")) as conn:
                    payload["database_ok"] = conn.execute(
                        "PRAGMA quick_check").fetchone()[0] == "ok"
            except sqlite3.Error:
                payload["database_ok"] = False
        alerts = []
        if (payload.get("disk_used_pct") or 0) > ALERT_THRESHOLDS["disk_used_pct"]:
            alerts.append("disk")
        if (payload.get("memory_used_pct") or 0) > ALERT_THRESHOLDS["memory_used_pct"]:
            alerts.append("memory")
        if payload.get("database_ok") is False:
            alerts.append("database")
        payload["alerts"] = alerts
        self.repository.record_system_status(payload)
        if alerts:
            self.repository.record_system_event("SYSTEM_ALERT", {"alerts": alerts})
        return payload
