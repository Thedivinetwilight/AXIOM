"""Bounded autonomous research loop + session-aware scheduling (spec §96/§97).

The mature AXIOM cycle is:

    Observe → Detect → Explain → Compare → Hypothesize → Test → Validate →
    Remember → Recommend → Monitor → Review → Improve

This module never runs that cycle by itself — it is the *governor* for it:

* a **daily experiment quota** so experiment generation cannot run away;
* **resource-pressure admission** via the compute governor (§26);
* **trading-critical awareness**: while a live session is active (or the
  governor is in a trading-critical window) large research work is refused,
  opportunity analysis of ACTIVE opportunities stays admissible, and
  reconciliation is never starved (§97);
* every refusal carries the reason — a refused experiment is recorded,
  never silently dropped or faked as started.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

LOOP_STEPS = ("OBSERVE", "DETECT", "EXPLAIN", "COMPARE", "HYPOTHESIZE",
              "TEST", "VALIDATE", "REMEMBER", "RECOMMEND", "MONITOR",
              "REVIEW", "IMPROVE")

#: research kinds that remain admissible inside trading-critical windows —
#: they serve the live operation instead of competing with it (§97).
CRITICAL_SAFE_KINDS = frozenset({"opportunity_analysis", "reconciliation_check"})

VERDICT_ADMITTED = "ADMITTED"
VERDICT_QUOTA = "REFUSED_QUOTA_EXCEEDED"
VERDICT_PRESSURE = "REFUSED_RESOURCE_PRESSURE"
VERDICT_CRITICAL = "REFUSED_TRADING_CRITICAL_WINDOW"


def _day_key(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class ResearchScheduler:
    """Quota + pressure + session-window admission for research work."""

    daily_experiment_quota: int = 8
    governor: object | None = None            # ResourceGovernor-shaped
    live_active: object | None = None         # () -> bool
    market_open: object | None = None         # () -> bool
    clock: object = time.time
    _started: dict = field(default_factory=dict)   # day -> count
    _refusals: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.daily_experiment_quota < 0:
            raise ValueError("daily_experiment_quota cannot be negative")

    # ------------------------------------------------------------- windows --
    def trading_critical_window(self, now: float | None = None) -> bool:
        """True when execution health outranks research throughput (§97)."""
        if self.governor is not None and getattr(self.governor, "trading_critical", False):
            return True
        if self.live_active is not None:
            try:
                if bool(self.live_active()):
                    return True
            except Exception:
                # An unreadable live state is treated as critical: research
                # yields, execution is protected (fail-safe direction).
                return True
        return False

    # ----------------------------------------------------------- admission --
    def admit(self, kind: str = "backtest", *, now: float | None = None) -> dict:
        """Admission decision for one research job. Recording a started
        experiment consumes quota only when admitted."""
        now = self.clock() if now is None else now
        kind = str(kind or "backtest").strip() or "backtest"
        day = _day_key(now)
        used = self._started.get(day, 0)
        critical = self.trading_critical_window(now)

        def refuse(verdict: str, reason: str) -> dict:
            decision = {"admitted": False, "verdict": verdict, "kind": kind,
                        "day": day, "quota_used": used,
                        "quota_limit": self.daily_experiment_quota,
                        "trading_critical_window": critical, "reason": reason,
                        "loop_steps": list(LOOP_STEPS)}
            self._refusals.append({"ts": now, **decision})
            if len(self._refusals) > 200:
                del self._refusals[: len(self._refusals) - 200]
            return decision

        if critical and kind not in CRITICAL_SAFE_KINDS:
            return refuse(VERDICT_CRITICAL,
                          "execution is active; large research work yields "
                          "to the hot path (only opportunity analysis and "
                          "reconciliation checks stay admissible)")
        if self.governor is not None and \
                getattr(self.governor, "under_pressure", lambda: False)():
            return refuse(VERDICT_PRESSURE,
                          "resource governor reports pressure; research "
                          "admission pauses until the host recovers")
        if used >= self.daily_experiment_quota:
            return refuse(VERDICT_QUOTA,
                          f"daily experiment quota exhausted "
                          f"({used}/{self.daily_experiment_quota}); the loop "
                          f"is bounded by design")
        self._started = {day: used + 1}
        return {"admitted": True, "verdict": VERDICT_ADMITTED, "kind": kind,
                "day": day, "quota_used": used + 1,
                "quota_limit": self.daily_experiment_quota,
                "trading_critical_window": critical,
                "loop_steps": list(LOOP_STEPS)}

    # -------------------------------------------------------------- status --
    def status(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        day = _day_key(now)
        used = self._started.get(day, 0)
        critical = self.trading_critical_window(now)
        expanded = (not critical) and used < self.daily_experiment_quota
        return {"day": day, "quota_used": used,
                "quota_limit": self.daily_experiment_quota,
                "trading_critical_window": critical,
                "market_open": bool(self.market_open()) if self.market_open else None,
                "policy": {
                    "expand_historical_research": expanded,
                    "run_backtests": expanded,
                    "train_or_evaluate_models": expanded,
                    "refresh_reports": expanded,
                    "opportunity_analysis": True,
                    "reconciliation_protection": True},
                "recent_refusals": self._refusals[-5:],
                "loop_steps": list(LOOP_STEPS),
                "note": "the loop is bounded: quotas, resource pressure and "
                        "trading-critical windows outrank research throughput"}
