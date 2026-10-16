"""Shared fail-closed checks for live readiness evidence."""

from __future__ import annotations

import math
from datetime import datetime, timezone


def reconciliation_freshness(repository, *, max_age_seconds: float,
                             now_epoch: float | None = None) -> dict:
    """Validate a recent, timezone-aware successful broker reconciliation.

    Missing, malformed, future-dated, unreadable, or stale evidence is not a
    successful reconciliation. A small one-second future tolerance covers
    clock skew; larger offsets remain UNKNOWN and block live entries.
    """
    try:
        maximum_age = float(max_age_seconds)
        now = (datetime.now(timezone.utc).timestamp() if now_epoch is None
               else float(now_epoch))
        if (not math.isfinite(maximum_age) or maximum_age <= 0
                or not math.isfinite(now) or now <= 0):
            return {"ok": False, "reason": "reconciliation freshness bound is invalid",
                    "age_seconds": None, "timestamp": None}
        state = repository.get_engine_state("last_reconcile_at")
        if not isinstance(state, dict):
            return {"ok": False, "reason": "successful reconciliation record is missing",
                    "age_seconds": None, "timestamp": None}
        stamp = state.get("ts")
        status = repository.get_engine_state("live_reconcile_status")
        if (not isinstance(status, dict) or status.get("ok") is not True
                or status.get("ts") != stamp):
            return {"ok": False,
                    "reason": "latest reconciliation did not prove success",
                    "age_seconds": None,
                    "timestamp": stamp if isinstance(stamp, str) else None}
        if not isinstance(stamp, str) or not stamp.strip():
            return {"ok": False, "reason": "no successful reconciliation timestamp",
                    "age_seconds": None, "timestamp": None}
        parsed = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return {"ok": False,
                    "reason": "reconciliation timestamp has no timezone",
                    "age_seconds": None, "timestamp": stamp}
        timestamp = parsed.timestamp()
        age = now - timestamp
        if not math.isfinite(age):
            return {"ok": False, "reason": "reconciliation age is invalid",
                    "age_seconds": None, "timestamp": stamp}
        if age < -1.0:
            return {"ok": False,
                    "reason": "reconciliation timestamp is in the future",
                    "age_seconds": age, "timestamp": stamp}
        if age > maximum_age:
            return {"ok": False,
                    "reason": "reconciliation is stale",
                    "age_seconds": age, "timestamp": stamp}
        return {"ok": True, "reason": "reconciliation is fresh",
                "age_seconds": max(0.0, age), "timestamp": stamp}
    except Exception as exc:
        return {"ok": False,
                "reason": f"reconciliation evidence is unreadable ({type(exc).__name__})",
                "age_seconds": None, "timestamp": None}
