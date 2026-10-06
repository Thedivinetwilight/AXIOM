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

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from threading import Lock

logger = logging.getLogger(__name__)

#: engine_state key holding the durable per-day quota usage (FINAL-11/10 §37:
#: a restart or crash must never reset the research quota).
QUOTA_STATE_KEY = "research_quota_usage"

#: how many days of quota history stay in the durable record
_QUOTA_HISTORY_DAYS = 14

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
    #: FINAL-11/10 §37: with a repository the per-day counters are DURABLE —
    # restarting or crashing cannot reset the quota. Without one (unit
    # tests), behaviour stays in-memory and byte-for-byte identical.
    repository: object | None = None
    _started: dict = field(default_factory=dict)   # day -> count
    _refusals: list = field(default_factory=list)
    _quota_lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.daily_experiment_quota < 0:
            raise ValueError("daily_experiment_quota cannot be negative")

    # -------------------------------------------- durable quota state (§37) --
    @staticmethod
    def _validate_usage(stored) -> dict | None:
        """Return a validated day->count map, or None for corrupt state."""
        if stored is None:
            return {}
        if not isinstance(stored, dict):
            return None
        usage: dict[str, int] = {}
        for key, value in stored.items():
            if not isinstance(key, str):
                return None
            try:
                parsed_day = date.fromisoformat(key)
            except ValueError:
                return None
            if parsed_day.isoformat() != key or type(value) is not int or value < 0:
                return None
            usage[key] = value
        return usage

    def _durable_usage(self) -> dict | None:
        """Read and validate persisted usage; corruption fails closed."""
        if self.repository is None:
            return None
        try:
            stored = self.repository.get_engine_state(QUOTA_STATE_KEY, None)
            usage = self._validate_usage(stored)
        except Exception:
            usage = None
        if usage is None:
            logger.warning("research quota state unreadable or malformed; "
                           "treating the quota as exhausted (fail closed)")
            return {"__unreadable__": 1}
        return usage

    def _reserve_durable_usage(self, day: str) -> tuple[int, str | None]:
        """Atomically consume one quota slot across threads and processes.

        Returns ``(usage_after, error)``. The row read, cap check and update
        share one ``BEGIN IMMEDIATE`` transaction, so concurrent workers cannot
        both spend the same final slot or lose an increment.
        """
        database = getattr(self.repository, "db", None)
        if database is None:
            return self.daily_experiment_quota, "durable quota storage is unavailable"
        try:
            with database.transaction() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM engine_state WHERE key=?",
                    (QUOTA_STATE_KEY,),
                ).fetchone()
                if row is None:
                    usage = {}
                else:
                    try:
                        stored = json.loads(row["payload"])
                    except (TypeError, ValueError):
                        return self.daily_experiment_quota, \
                            "durable quota state is unreadable"
                    usage = self._validate_usage(stored)
                    if usage is None:
                        return self.daily_experiment_quota, \
                            "durable quota state is malformed"
                used = usage.get(day, 0)
                if used >= self.daily_experiment_quota:
                    return used, "daily experiment quota exhausted"
                usage[day] = used + 1
                trimmed = dict(sorted(usage.items())[-_QUOTA_HISTORY_DAYS:])
                payload = json.dumps(trimmed, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False)
                db.execute(
                    "INSERT INTO engine_state(key,payload) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                    (QUOTA_STATE_KEY, payload),
                )
                return used + 1, None
        except Exception as exc:
            logger.error("research quota reservation failed; refusing admission",
                         extra={"error_type": type(exc).__name__})
            return self.daily_experiment_quota, \
                f"durable quota could not be persisted ({type(exc).__name__})"

    def _usage_for(self, day: str) -> tuple[int, bool]:
        """(used, bounded) for ``day``. bounded=True when the durable record
        is unreadable — quota exhaustion is then the only honest answer."""
        durable = self._durable_usage()
        if durable is None:
            return int(self._started.get(day, 0)), False
        if durable.pop("__unreadable__", None):
            return self.daily_experiment_quota, True
        return int(durable.get(day, self._started.get(day, 0))), False

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
        used, unreadable = self._usage_for(day)
        critical = self.trading_critical_window(now)

        def refuse(verdict: str, reason: str, *, quota_used: int | None = None) -> dict:
            decision = {"admitted": False, "verdict": verdict, "kind": kind,
                        "day": day,
                        "quota_used": used if quota_used is None else quota_used,
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
        if unreadable:
            return refuse(VERDICT_QUOTA,
                          "the durable quota record is unreadable or malformed; refusing "
                          "rather than assuming fresh quota (a restart can "
                          "never buy quota)")
        if used >= self.daily_experiment_quota:
            return refuse(VERDICT_QUOTA,
                          f"daily experiment quota exhausted "
                          f"({used}/{self.daily_experiment_quota}); the loop "
                          f"is bounded by design")
        if self.repository is not None:
            used_after, reservation_error = self._reserve_durable_usage(day)
            if reservation_error is not None:
                reason = (
                    reservation_error if reservation_error !=
                    "daily experiment quota exhausted" else
                    f"daily experiment quota exhausted "
                    f"({used_after}/{self.daily_experiment_quota}); the loop is bounded by design")
                return refuse(VERDICT_QUOTA, reason, quota_used=used_after)
        else:
            with self._quota_lock:
                current_used = int(self._started.get(day, 0))
                if current_used >= self.daily_experiment_quota:
                    return refuse(
                        VERDICT_QUOTA,
                        f"daily experiment quota exhausted "
                        f"({current_used}/{self.daily_experiment_quota}); the loop "
                        f"is bounded by design",
                        quota_used=current_used)
                used_after = current_used + 1
                self._started[day] = used_after
        with self._quota_lock:
            self._started[day] = max(int(self._started.get(day, 0)), used_after)
        return {"admitted": True, "verdict": VERDICT_ADMITTED, "kind": kind,
                "day": day, "quota_used": used_after,
                "quota_limit": self.daily_experiment_quota,
                "trading_critical_window": critical,
                "loop_steps": list(LOOP_STEPS)}

    # -------------------------------------------------------------- status --
    def status(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        day = _day_key(now)
        used, unreadable = self._usage_for(day)
        if unreadable:
            used = self.daily_experiment_quota
        critical = self.trading_critical_window(now)
        expanded = (not critical) and used < self.daily_experiment_quota
        return {"day": day, "quota_used": used,
                "quota_limit": self.daily_experiment_quota,
                "durable": self.repository is not None,
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
