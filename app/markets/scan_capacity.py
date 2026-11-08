"""Quota-aware scan capacity planning for a 100-symbol read-only scan.

What this module is
-------------------
A small, pure planning layer between the documented provider quota profiles
(:mod:`app.markets.fetch_gate`) and the operator console. Given

* the market and its quota profile (Upstox standard APIs / Alpaca market data),
* how many symbols the operator wants to scan, and
* the cadence they want to hold,

it computes how many provider requests one cycle costs, the slowest-cadence
constraint each documented window imposes, how many symbols the requested
cadence can actually support, and whether the current rolling-window usage has
room for one more cycle *now*. Everything is derived from the same
:class:`~app.markets.fetch_gate.QuotaPolicy` objects the fetch gate enforces, so
the promise shown in the UI cannot drift from what the gate will do.

What this module is not
-----------------------
It never fetches, never submits an order, never arms or activates anything and
never touches a credential. Planning is offline: :func:`plan_capacity` is pure
arithmetic over the documented windows and a snapshot of gate usage. Only
:class:`ScanCapacityService.start_scan` triggers market-data traffic, and that
path calls the existing read-only ensemble evaluation with ``execute_in_paper``
forced off, so it cannot place a paper or broker order.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.markets.fetch_gate import (
    MARKET_ENDPOINT_CATEGORIES,
    ProviderFetchGate,
    QuotaPolicy,
)

__all__ = [
    "CapacityPlan",
    "ScanCapacityService",
    "ScanRun",
    "plan_capacity",
]

# Status vocabulary shown verbatim in the operator console (text, not colour).
STATUS_READY = "READY"
STATUS_SLOWER_CADENCE_REQUIRED = "SLOWER CADENCE REQUIRED"
STATUS_QUOTA_DEFERRED = "QUOTA DEFERRED"
STATUS_NO_CREDENTIAL = "NO CREDENTIAL"
STATUS_PLAN_UNVERIFIED = "PLAN UNVERIFIED"
STATUS_PROVIDER_ERROR = "PROVIDER ERROR"
STATUS_CONFIG_REQUIRED = "CONFIG REQUIRED"
STATUS_SCAN_IN_PROGRESS = "SCAN IN PROGRESS"
STATUS_SCANNING = "SCANNING"

MAX_SCAN_SYMBOLS = 100
MIN_SCAN_SYMBOLS = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def endpoints_per_category(market: str) -> dict[str, int]:
    """How many of the scan's per-symbol requests fall in each quota category.

    A scan asks one bar series and one quote per symbol. When both endpoints are
    separate documented APIs (Upstox) each category gets one request per symbol;
    when the provider publishes one market-data pool (Alpaca) both land in it.
    """
    endpoints = MARKET_ENDPOINT_CATEGORIES.get(
        str(market).upper(), MARKET_ENDPOINT_CATEGORIES["NSE"]
    )
    counts: dict[str, int] = {}
    for category in endpoints.values():
        counts[category] = counts.get(category, 0) + 1
    return counts


def requests_per_cycle(market: str, symbols: int) -> dict[str, int]:
    """Provider requests one scan cycle of ``symbols`` costs, per category."""
    return {
        category: int(count) * max(0, int(symbols))
        for category, count in endpoints_per_category(market).items()
    }


def min_interval_seconds(market: str, policies: Sequence[QuotaPolicy], symbols: int) -> float:
    """Slowest documented window's constraint on the cycle interval."""
    per_cycle = requests_per_cycle(market, symbols)
    worst = 0.0
    for policy in policies:
        count = max(1, per_cycle.get(policy.category, symbols))
        for window in policy.windows:
            worst = max(worst, (window.seconds * count) / max(1, window.limit))
    return round(worst, 3)


def max_symbols_at_cadence(market: str, policies: Sequence[QuotaPolicy], cadence_s: float) -> int:
    """How many symbols a cadence can support under every documented window."""
    cadence = max(0.0, float(cadence_s))
    per_symbol = endpoints_per_category(market)
    best: int | None = None
    for policy in policies:
        factor = max(1, per_symbol.get(policy.category, 1))
        for window in policy.windows:
            allowed = int((window.limit * cadence) / (window.seconds * factor))
            best = allowed if best is None else min(best, allowed)
    return max(0, best if best is not None else 0)


# Windows at or below this length are pacing windows: the gate's token bucket
# spreads a cycle across several seconds instead of admitting it instantly, so
# such a window cannot block a cycle. Longer windows are rolling budgets: a
# cycle that does not fit has to be deferred, never burst through.
PACING_WINDOW_MAX_SECONDS = 5.0


def _window_label(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g} second"
    if seconds < 3600:
        return f"{seconds / 60:g} minute"
    return f"{seconds / 3600:g} hour"


def _window_rows(
    policy: QuotaPolicy,
    usage: Mapping[str, int],
    requests: int,
) -> list[dict[str, Any]]:
    """Per-window rows: current use, the cycle's cost, and the resulting verdict."""
    rows: list[dict[str, Any]] = []
    for window in policy.windows:
        current = int(usage.get(window.name, 0))
        projected = current + requests
        fits = projected <= window.limit
        paced = window.seconds <= PACING_WINDOW_MAX_SECONDS
        # Uniform-ageing estimate of how long the gate must wait for room. Only
        # ever an estimate: the true value depends on the oldest live admission.
        wait = 0.0
        if not fits and window.limit > 0:
            overflow = projected - window.limit
            wait = round(overflow * window.seconds / window.limit, 3)
        rows.append(
            {
                "window": window.name,
                "seconds": window.seconds,
                "label": _window_label(window.seconds),
                "limit": window.limit,
                "current": current,
                "requests_this_cycle": requests,
                "projected": projected,
                "fits_now": fits,
                "paced": paced,
                "blocks": (not fits) and (not paced),
                "wait_seconds": wait,
                "headroom": window.limit - projected,
                "readable": (
                    f"{current:,} / {window.limit:,} requests this "
                    f"{_window_label(window.seconds)}"
                ),
            }
        )
    return rows


def dispatch_seconds(market: str, policies: Sequence[QuotaPolicy], symbols: int) -> float:
    """How long the gate needs to pace one cycle at the documented short-window rate."""
    per_cycle = endpoints_per_category(market)
    longest = 0.0
    for policy in policies:
        count = per_cycle.get(policy.category, 1) * max(0, int(symbols))
        if not count:
            continue
        # Pace at the documented short-window rate; a 30-minute budget says how
        # often a cycle may repeat, not how fast the gate releases one cycle.
        pacing = [
            w.limit / w.seconds
            for w in policy.windows
            if 0 < w.seconds <= PACING_WINDOW_MAX_SECONDS
        ]
        rates = pacing or [w.limit / w.seconds for w in policy.windows if w.seconds > 0]
        if rates:
            longest = max(longest, count / min(rates))
    return round(longest, 3)


@dataclass
class CapacityPlan:
    """Outcome of planning one scan against the documented quota windows."""

    market: str
    symbols: int
    cadence_s: float
    status: str
    can_start: bool
    reasons: list[str]
    min_interval_s: float
    max_symbols_at_cadence: int
    requests_per_cycle: dict[str, int]
    dispatch_seconds: float
    windows: dict[str, list[dict[str, Any]]]
    quota_profile: str
    credential_present: bool
    plan_state: str
    plan_detail: str
    blocking_window: str | None = None
    wait_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "symbols": self.symbols,
            "cadence_requested_seconds": self.cadence_s,
            "cadence_sustainable_seconds": self.min_interval_s,
            "max_symbols_at_requested_cadence": self.max_symbols_at_cadence,
            "requests_per_cycle": self.requests_per_cycle,
            "total_requests_per_cycle": sum(self.requests_per_cycle.values()),
            "expected_dispatch_seconds": self.dispatch_seconds,
            "status": self.status,
            "can_start": self.can_start,
            "reasons": list(self.reasons),
            "window_usage": self.windows,
            "blocking_window": self.blocking_window,
            "wait_seconds": self.wait_seconds,
            "quota_profile": self.quota_profile,
            "credential_present": self.credential_present,
            "plan_state": self.plan_state,
            "plan_detail": self.plan_detail,
        }


def plan_capacity(
    *,
    market: str,
    symbols: int,
    cadence_s: float,
    policies: Sequence[QuotaPolicy],
    quota_profile: str,
    usage: Mapping[str, Mapping[str, int]],
    credential_present: bool,
    plan_state: str,
    plan_detail: str,
    scan_busy: bool = False,
    provider_error: str = "",
) -> CapacityPlan:
    """Pure capacity arithmetic: no fetch, no quota consumption, no side effect."""
    mkt = str(market).upper()
    n = max(MIN_SCAN_SYMBOLS, min(MAX_SCAN_SYMBOLS, int(symbols)))
    cadence = max(1.0, float(cadence_s))
    per_cycle = requests_per_cycle(mkt, n)
    earliest = min_interval_seconds(mkt, policies, n)
    allowed_symbols = max_symbols_at_cadence(mkt, policies, cadence)
    dispatch = dispatch_seconds(mkt, policies, n)

    windows: dict[str, list[dict[str, Any]]] = {}
    reasons: list[str] = []
    blocking_window: str | None = None
    wait_seconds = 0.0
    can_start = True
    status = STATUS_READY

    for policy in policies:
        rows = _window_rows(policy, usage.get(policy.category, {}), per_cycle.get(policy.category, 0))
        windows[policy.category] = rows
        for row in rows:
            if row["blocks"]:
                # The cycle would have to wait for older requests to age out;
                # the gate defers instead of breaching the documented window.
                can_start = False
                status = STATUS_QUOTA_DEFERRED
                blocking_window = f"{policy.category} · {row['window']}"
                wait_seconds = max(wait_seconds, float(row["wait_seconds"]))
                reasons.append(
                    f"{policy.label}: this cycle would put {row['projected']:,} requests in the "
                    f"{row['label']} window (limit {row['limit']:,}); defer until "
                    f"~{row['wait_seconds']:.1f}s of the window has drained"
                )

    if cadence < earliest:
        can_start = False
        if status == STATUS_READY:
            status = STATUS_SLOWER_CADENCE_REQUIRED
        reasons.append(
            f"{mkt}: {n} symbols need at least {earliest:.1f}s between cycles "
            f"({allowed_symbols} symbols fit a {cadence:.0f}s cadence); "
            f"the documented windows are the binding constraint, not the CPU"
        )

    if not policies:
        can_start = False
        status = STATUS_CONFIG_REQUIRED
        reasons.append(
            f"{mkt}: no documented quota profile is enforced, so capacity cannot be "
            "modelled; set MARKET_DATA_QUOTA_PROFILE=documented before scanning"
        )

    if not credential_present:
        can_start = False
        status = STATUS_NO_CREDENTIAL
        reasons.append(f"{mkt}: read-only market-data credential is not configured")

    if plan_state != "verified" and status == STATUS_READY:
        # An unverified plan is not a blocker for a read-only scan, but the
        # operator must see that the numbers assume the Basic limits.
        status = STATUS_PLAN_UNVERIFIED
        reasons.append(plan_detail)

    if provider_error:
        can_start = False
        status = STATUS_PROVIDER_ERROR
        reasons.append(provider_error)

    if scan_busy:
        can_start = False
        status = STATUS_SCAN_IN_PROGRESS
        reasons.append(f"{mkt}: a read-only scan is already running")

    return CapacityPlan(
        market=mkt,
        symbols=n,
        cadence_s=cadence,
        status=status,
        can_start=can_start,
        reasons=reasons,
        min_interval_s=earliest,
        max_symbols_at_cadence=allowed_symbols,
        requests_per_cycle=per_cycle,
        dispatch_seconds=dispatch,
        windows=windows,
        quota_profile=quota_profile,
        credential_present=bool(credential_present),
        plan_state=plan_state,
        plan_detail=plan_detail,
        blocking_window=blocking_window,
        wait_seconds=round(wait_seconds, 3),
    )


@dataclass
class ScanRun:
    """One completed read-only scan cycle, as recorded for the console."""

    run_id: str
    market: str
    state: str
    started_at: str
    finished_at: str = ""
    symbols_selected: int = 0
    symbols_analyzed: int = 0
    symbols_stale: int = 0
    symbols_failed: int = 0
    symbols_deferred: int = 0
    candidates: int = 0
    orders: int = 0
    broker_order_api_calls: int = 0
    cadence_requested_s: float = 0.0
    cadence_achieved_s: float = 0.0
    cadence_met: bool = False
    elapsed_ms: float = 0.0
    calls_by_category: dict[str, int] = field(default_factory=dict)
    quota_after: dict[str, Any] = field(default_factory=dict)
    quota_windows_after: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "market": self.market,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "symbols_selected": self.symbols_selected,
            "symbols_analyzed": self.symbols_analyzed,
            "symbols_stale": self.symbols_stale,
            "symbols_failed": self.symbols_failed,
            "symbols_deferred": self.symbols_deferred,
            "candidates_risk_approved": self.candidates,
            "orders": self.orders,
            "broker_order_api_calls": self.broker_order_api_calls,
            "cadence_requested_seconds": self.cadence_requested_s,
            "cadence_achieved_seconds": self.cadence_achieved_s,
            "cadence_met": self.cadence_met,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "calls_by_category": dict(self.calls_by_category),
            "quota_after": dict(self.quota_after),
            "quota_windows_after": dict(self.quota_windows_after),
            "detail": self.detail,
            "error": self.error,
            "orders_submitted": 0,
            "live_execution": "DISABLED",
        }


class ScanCapacityService:
    """Planning + read-only scan orchestration for one running engine.

    The service holds no credentials. It reads credential *presence* from the
    callables the server passes in, and it only ever calls the read-only
    ``evaluate_watchlist_cycle`` with ``execute_in_paper=False``.
    """

    def __init__(
        self,
        *,
        gates: Mapping[str, ProviderFetchGate],
        policies: Mapping[str, tuple[QuotaPolicy, ...]],
        profiles: Mapping[str, str],
        plan_states: Mapping[str, tuple[str, str]],
        credential_present: Callable[[str], bool],
        evaluate: Callable[..., Mapping[str, Any]],
        limits: Callable[[str], Mapping[str, Any]] | None = None,
        watchlist: Callable[[str], list[str]] | None = None,
        history_limit: int = 40,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._gates = dict(gates)
        self._policies = dict(policies)
        self._profiles = dict(profiles)
        self._plan_states = dict(plan_states)
        self._credential_present = credential_present
        self._evaluate = evaluate
        self._limits = limits
        self._watchlist = watchlist
        self._history_limit = max(1, int(history_limit))
        self._clock = clock
        self._lock = threading.RLock()
        self._running: dict[str, str] = {}
        self._history: list[ScanRun] = []

    # -- provider/quota metadata -------------------------------------------
    def policies(self, market: str) -> tuple[QuotaPolicy, ...]:
        return tuple(self._policies.get(str(market).upper(), ()))

    def quota_profile_payload(self, market: str) -> dict[str, Any]:
        mkt = str(market).upper()
        state, detail = self._plan_states.get(mkt, ("verified", ""))
        return {
            "market": mkt,
            "profile": self._profiles.get(mkt, "none"),
            "plan_state": state,
            "plan_detail": detail,
            "verification": (
                "DOCUMENTED" if state == "verified" else "UNVERIFIED"
            ),
            "credential_present": bool(self._credential_present(mkt)),
            "policies": [p.to_dict() for p in self.policies(mkt)],
            "endpoints_per_category": endpoints_per_category(mkt),
        }

    def gate_stats(self, market: str) -> dict[str, Any]:
        gate = self._gates.get(str(market).upper())
        return gate.stats() if gate is not None else {}

    def usage(self, market: str) -> dict[str, dict[str, int]]:
        gate = self._gates.get(str(market).upper())
        if gate is None:
            return {}
        out: dict[str, dict[str, int]] = {}
        for policy in self.policies(market):
            out[policy.category] = gate.quota_usage(policy.category)
        return out

    # -- planning ----------------------------------------------------------
    def plan(
        self,
        market: str,
        *,
        symbols: int,
        cadence_s: float,
    ) -> CapacityPlan:
        mkt = str(market).upper()
        state, detail = self._plan_states.get(mkt, ("verified", ""))
        return plan_capacity(
            market=mkt,
            symbols=symbols,
            cadence_s=cadence_s,
            policies=self.policies(mkt),
            quota_profile=self._profiles.get(mkt, "none"),
            usage=self.usage(mkt),
            credential_present=bool(self._credential_present(mkt)),
            plan_state=state,
            plan_detail=detail,
            scan_busy=mkt in self._running,
        )

    def snapshot(self, market: str, *, symbols: int, cadence_s: float) -> dict[str, Any]:
        mkt = str(market).upper()
        plan = self.plan(mkt, symbols=symbols, cadence_s=cadence_s)
        gate_stats = self.gate_stats(mkt)
        return {
            "market": mkt,
            "quota": self.quota_profile_payload(mkt),
            "plan": plan.to_dict(),
            "gate": {
                key: gate_stats.get(key)
                for key in (
                    "requests",
                    "retries",
                    "rate_limited_events",
                    "cooldowns",
                    "deferred",
                    "quota_waits",
                    "quota_wait_seconds",
                    "effective_requests_per_second",
                    "peak_inflight",
                    "workers_used",
                )
            },
            "limits": dict(self._limits(mkt) if self._limits else {}),
            "selected_symbols": (
                len(self._watchlist(mkt)) if self._watchlist is not None else None
            ),
            "scan_running": mkt in self._running,
        }

    # -- read-only scan ----------------------------------------------------
    def start(
        self,
        market: str,
        *,
        symbols: Iterable[str] | None,
        cadence_s: float,
        timeframe: str | None = None,
    ) -> dict[str, Any]:
        """Start one read-only scan, or refuse with the reason it cannot run."""
        mkt = str(market).upper()
        wanted = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
        if not wanted:
            if self._watchlist is None:
                raise ValueError("No symbols selected and no saved watchlist is available")
            wanted = [str(s).upper() for s in self._watchlist(mkt)]
        if not wanted:
            raise ValueError(f"No selected symbols for {mkt}")
        if len(wanted) > MAX_SCAN_SYMBOLS:
            raise ValueError(
                f"This workflow scans at most {MAX_SCAN_SYMBOLS} symbols per cycle "
                f"(requested {len(wanted)}); split the universe or reduce the selection"
            )

        plan = self.plan(mkt, symbols=len(wanted), cadence_s=cadence_s)
        if not plan.can_start:
            return {
                "started": False,
                "status": plan.status,
                "plan": plan.to_dict(),
                "detail": "; ".join(plan.reasons) or plan.status,
            }

        with self._lock:
            if mkt in self._running:
                return {
                    "started": False,
                    "status": STATUS_SCAN_IN_PROGRESS,
                    "plan": plan.to_dict(),
                    "detail": f"{mkt}: a read-only scan is already running",
                }
            run = ScanRun(
                run_id=uuid.uuid4().hex[:12],
                market=mkt,
                state=STATUS_SCANNING,
                started_at=self._clock().isoformat(),
                symbols_selected=len(wanted),
                cadence_requested_s=float(cadence_s),
            )
            self._running[mkt] = run.run_id
            self._history.insert(0, run)
            del self._history[self._history_limit :]

        thread = threading.Thread(
            target=self._execute,
            args=(run, wanted, timeframe),
            name=f"axiom-scan-{mkt.lower()}",
            daemon=True,
        )
        thread.start()
        return {
            "started": True,
            "status": STATUS_SCANNING,
            "run_id": run.run_id,
            "plan": plan.to_dict(),
            "detail": (
                f"{mkt}: read-only scan of {len(wanted)} symbols started; "
                "0 orders will be submitted"
            ),
        }

    def _execute(self, run: ScanRun, symbols: list[str], timeframe: str | None) -> None:
        gate = self._gates.get(run.market)
        before_calls: dict[str, int] = {}
        if gate is not None:
            report = gate.quota_report()
            for category, payload in (report.get("categories") or {}).items():
                before_calls[category] = int((payload or {}).get("requests", 0))
        started = time.perf_counter()
        try:
            result = self._evaluate(
                run.market,
                symbols=symbols,
                timeframe=timeframe,
                execute_in_paper=False,
                persist_decisions=False,
            )
            decisions = list(result.get("decisions") or [])
            rejected = dict(result.get("rejected_instruments") or {})
            deferred = sum(
                1
                for payload in rejected.values()
                if str((payload or {}).get("code") or "").upper() == "QUOTA_DEFERRED"
            )
            stale = sum(
                1
                for payload in rejected.values()
                if str((payload or {}).get("code") or "").upper() in {"STALE_DATA", "STALE_QUOTE"}
            )
            failed = len(rejected) - deferred - stale
            candidates = sum(
                1
                for dec in decisions
                if str(dec.get("final_action") or "").upper() in {"BUY", "SELL"}
                and int(dec.get("selected_qty") or 0) > 0
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._lock:
                run.state = "COMPLETED"
                run.finished_at = self._clock().isoformat()
                run.symbols_analyzed = len(decisions)
                run.symbols_stale = stale
                run.symbols_failed = max(0, failed)
                run.symbols_deferred = deferred
                run.candidates = candidates
                run.orders = 0
                run.broker_order_api_calls = int(result.get("broker_order_api_calls") or 0)
                run.elapsed_ms = elapsed_ms
                run.cadence_achieved_s = round(elapsed_ms / 1000.0, 3)
                run.cadence_met = run.cadence_achieved_s <= run.cadence_requested_s
                run.detail = (
                    f"{len(decisions)} analyzed, {candidates} risk-approved candidate(s), 0 orders"
                )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator, never raised
            with self._lock:
                run.state = STATUS_PROVIDER_ERROR
                run.finished_at = self._clock().isoformat()
                run.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                run.elapsed_ms = (time.perf_counter() - started) * 1000.0
        finally:
            if gate is not None:
                report = gate.quota_report()
                calls: dict[str, int] = {}
                after_usage: dict[str, Any] = {}
                for category, payload in (report.get("categories") or {}).items():
                    payload = payload or {}
                    calls[category] = max(
                        0, int(payload.get("requests", 0)) - int(before_calls.get(category, 0))
                    )
                    after_usage[category] = {
                        name: int((row or {}).get("current", 0))
                        for name, row in (payload.get("windows") or {}).items()
                    }
                with self._lock:
                    run.calls_by_category = calls
                    run.quota_after = after_usage
                    run.quota_windows_after = report.get("categories", {})
                    self._running.pop(run.market, None)
            else:
                with self._lock:
                    self._running.pop(run.market, None)

    # -- history -----------------------------------------------------------
    def history(self, market: str = "", *, limit: int = 20) -> list[dict[str, Any]]:
        mkt = str(market or "").upper()
        with self._lock:
            rows = [r for r in self._history if not mkt or r.market == mkt]
            return [copy.deepcopy(r.to_dict()) for r in rows[: max(1, min(100, int(limit)))]]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": {mkt: run_id for mkt, run_id in self._running.items()},
                "history_count": len(self._history),
                "max_symbols_per_scan": MAX_SCAN_SYMBOLS,
            }
