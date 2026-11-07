#!/usr/bin/env python3
"""Offline capacity benchmark for scanning 100 selected symbols in one cycle.

What this measures
------------------
The benchmark drives the *real* code paths of the multi-market paper path:

    provider fetch -> ``LiveMarketPaperEngine.analyze_market`` (scan)
    -> ``StrategyEnsembleService.evaluate_watchlist_cycle`` (24-variant ensemble
       evaluation per symbol, bounded parallel)
    -> ``PortfolioRiskAllocator.allocate_symbol`` (portfolio risk checks, sizing)
    -> ``LiveMarketPaperEngine.submit_order`` (paper order planning)

Every provider transport is a local fake: deterministic synthetic OHLCV and
quotes, injectable latency, an optional provider-side throttle that answers
``HTTP 429``, and injectable per-symbol failures. **No socket is opened and no
broker order endpoint is reachable**: the execution boundary
(``app.markets.live_execution``) is not constructed at all, the engine runs in
PAPER mode, and ``broker_order_api_calls`` stays 0 by construction.

What this does *not* measure
----------------------------
It cannot say how a real broker behaves. Real provider latency, real rate-limit
entitlements, exchange session behaviour, corporate-action data quality and fill
quality are all unverified here; a fast cycle in this harness is a statement
about AXIOM's own CPU/IO fan-out, not about Upstox or Alpaca.

Usage
-----
    python scripts/benchmark_market_scan.py                 # full table, 100 symbols
    python scripts/benchmark_market_scan.py --symbols 16    # the old grid, for comparison
    python scripts/benchmark_market_scan.py --json docs/MARKET_SCAN_CAPACITY.json
    python scripts/benchmark_market_scan.py --quick         # small, fast smoke run
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import threading
import time
import tracemalloc
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # pragma: no cover - script convenience
    sys.path.insert(0, str(ROOT))

from app.data.models import Candle  # noqa: E402
from app.ensemble.service import StrategyEnsembleService  # noqa: E402
from app.markets.fetch_gate import (  # noqa: E402
    MARKET_ENDPOINT_CATEGORIES,
    FetchGateConfig,
    ProviderFetchGate,
    QuotaPolicy,
    alpaca_market_data_policies,
    upstox_standard_policies,
)
from app.markets.live_paper import LiveMarketPaperEngine  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
# Monday 2026-01-05 10:00 IST: NSE regular session (09:15-15:30), US session closed.
SESSION_NOW = datetime(2026, 1, 5, 10, 0, tzinfo=IST)
BENCH_TF = "5m"
BARS_PER_SYMBOL = 220


# --------------------------------------------------------------------------
# Deterministic synthetic provider transport (no network, no broker)
# --------------------------------------------------------------------------
def _seed_for(symbol: str) -> int:
    """Stable per-symbol seed: ``hash()`` is salted per process, crc32 is not."""
    return zlib.crc32(str(symbol).encode("utf-8"))


def _base_price(symbol: str) -> float:
    return 100.0 + (_seed_for(symbol) % 400)


class FakeMarketData:
    """Local fake of the per-symbol quote/bar providers.

    ``latency_ms``            simulated round trip per request
    ``provider_rps``          requests/second the fake "provider" accepts before
                              answering HTTP 429 (0 disables the throttle)
    ``failures``              symbol -> mode in {error, timeout, empty_bars,
                              stale_quote, rate_limit}
    """

    def __init__(
        self,
        *,
        latency_ms: float = 0.0,
        provider_rps: float = 0.0,
        failures: dict[str, str] | None = None,
        trend: str = "up",
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self.latency = max(0.0, float(latency_ms)) / 1000.0
        self.provider_rps = float(provider_rps or 0.0)
        self.failures = dict(failures or {})
        # Injectable time source: the 30-minute schedule simulation runs on a
        # virtual clock so 30 simulated minutes cost seconds of real time.
        self._sleep = sleep_fn or time.sleep
        self._now = clock_fn or time.monotonic
        self.trend = trend
        self._lock = threading.Lock()
        self._window_start = self._now()
        self._window_count = 0
        self.calls = 0
        self.quote_calls = 0
        self.bar_calls = 0
        self.rate_limited = 0
        self.failures_served = 0
        self.latencies_ms: list[float] = []
        self.inflight = 0
        self.peak_inflight = 0
        self.per_symbol_calls: dict[str, int] = {}

    # -- bookkeeping -------------------------------------------------------
    def _enter(self, symbol: str, kind: str) -> tuple[str, str]:
        with self._lock:
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
            self.calls += 1
            if kind == "quote":
                self.quote_calls += 1
            else:
                self.bar_calls += 1
            canon = str(symbol).split("|")[-1].upper()
            self.per_symbol_calls[canon] = self.per_symbol_calls.get(canon, 0) + 1
            throttled = False
            if self.provider_rps > 0:
                now = self._now()
                if now - self._window_start >= 1.0:
                    self._window_start = now
                    self._window_count = 0
                self._window_count += 1
                throttled = self._window_count > self.provider_rps
                if throttled:
                    self.rate_limited += 1
            # Quote providers are called with the provider instrument key
            # (``NSE_EQ|SYN001``) while bars use the bare symbol: key both forms on
            # the bare symbol so a failure mode applies to the whole symbol.
            mode = str(self.failures.get(str(symbol).split("|")[-1].upper()) or "")
            if mode:
                self.failures_served += 1
        return ("rate_limit" if throttled else mode), kind

    def _exit(self, started: float) -> None:
        with self._lock:
            self.inflight -= 1
            self.latencies_ms.append(max(0.0, (self._now() - started) * 1000.0))

    # -- data --------------------------------------------------------------
    def _bars(self, symbol: str) -> list[Candle]:
        """Deterministic series.

        ``up``       identical shape for every symbol (perfectly correlated: the
                     portfolio correlation gate is what refuses to hold them all)
        ``diverse``  per-symbol drift/phase/amplitude, so several decorrelated
                     names can pass the risk layer and the position cap can bind
        ``chop``     no trend: the signal/confidence gates refuse
        """
        base = _base_price(symbol)
        seed = _seed_for(symbol)
        drift = (seed % 7) * 0.0001
        out: list[Candle] = []
        for idx in range(BARS_PER_SYMBOL):
            ts = SESSION_NOW - timedelta(minutes=5 * (BARS_PER_SYMBOL - idx))
            step = 1.0 + (0.0025 + drift) * idx
            if self.trend == "chop":
                price = base * (1.0 + 0.002 * ((idx * 7) % 11 - 5))
            elif self.trend == "updown":
                price = base * step if (idx // 40) % 2 == 0 else base * step * 0.94
            elif self.trend == "diverse":
                rate = 0.002 + ((seed >> 3) % 5) * 0.0008
                phase = (seed >> 7) % 37
                amp = 0.004 + ((seed >> 11) % 5) * 0.003
                price = base * (1.0 + rate * idx) * (1.0 + amp * math.sin((idx + phase) / 5.0))
            else:
                price = base * step
            price = round(price, 2)
            out.append(
                Candle(
                    timestamp=ts,
                    open=price,
                    high=round(price * 1.002, 2),
                    low=round(price * 0.998, 2),
                    close=price,
                    volume=float(60_000 + (_seed_for(symbol) % 40_000)),
                    symbol=symbol,
                )
            )
        return out

    def _quote(self, symbol: str, stale: bool = False) -> dict[str, Any]:
        price = _base_price(symbol)
        stamp = SESSION_NOW - timedelta(hours=2 if stale else 0)
        return {
            "instrument_key": str(symbol),
            "bid": round(price - 0.05, 2),
            "ask": round(price + 0.05, 2),
            "last": price,
            "volume": 120_000,
            "timestamp": stamp.isoformat(),
            "source_timestamp": stamp.isoformat(),
            "local_received_at": SESSION_NOW.isoformat(),
            "feed": "benchmark-synthetic",
            "feed_entitlement_label": "SYNTHETIC (no broker feed)",
            "is_delayed": False,
            "source": "benchmark-fake-transport",
        }

    # -- provider callables ------------------------------------------------
    def quote(self, symbol: str) -> dict[str, Any]:
        mode, _ = self._enter(symbol, "quote")
        started = self._now()
        try:
            if self.latency:
                self._sleep(self.latency)
            if mode == "rate_limit":
                raise RuntimeError("HTTP 429 Too Many Requests (synthetic provider throttle)")
            if mode in {"error", "timeout"}:
                raise TimeoutError(f"synthetic provider timeout for {symbol}") if mode == "timeout" else RuntimeError(
                    f"synthetic provider error for {symbol}"
                )
            return self._quote(str(symbol), stale=(mode == "stale_quote"))
        finally:
            self._exit(started)

    def bars(self, symbol: str, timeframe: str) -> list[Candle]:
        mode, _ = self._enter(symbol, "bars")
        started = self._now()
        try:
            if self.latency:
                self._sleep(self.latency)
            if mode == "rate_limit":
                raise RuntimeError("HTTP 429 Too Many Requests (synthetic provider throttle)")
            if mode == "empty_bars":
                return []
            if mode in {"error", "timeout"}:
                raise TimeoutError(f"synthetic provider timeout for {symbol}") if mode == "timeout" else RuntimeError(
                    f"synthetic provider error for {symbol}"
                )
            return self._bars(str(symbol))
        finally:
            self._exit(started)

    def latency_summary(self) -> dict[str, float]:
        with self._lock:
            values = sorted(self.latencies_ms)
        if not values:
            return {"count": 0}
        return {
            "count": len(values),
            "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 3),
            "max_ms": round(values[-1], 3),
        }

    def reset_counters(self) -> None:
        with self._lock:
            self.calls = 0
            self.quote_calls = 0
            self.bar_calls = 0
            self.rate_limited = 0
            self.failures_served = 0
            self.latencies_ms = []
            self.peak_inflight = 0
            self.per_symbol_calls = {}


def _resolver(symbol: str) -> dict[str, Any]:
    text = str(symbol).split("|")[-1].upper()
    return {
        "trading_symbol": text,
        "symbol": text,
        "instrument_key": f"NSE_EQ|{text}",
        "tick_size": 0.05,
        "lot_size": 1,
        "instrument_type": "EQ",
        "product": "D",
    }


def _rss_mb() -> float:
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except Exception:  # pragma: no cover - platform dependent
        return 0.0


# --------------------------------------------------------------------------
# Benchmark arms
# --------------------------------------------------------------------------
def quota_policies_for(profile: str) -> tuple[QuotaPolicy, ...]:
    """Documented quota policies for a named profile (empty = no windows)."""
    key = (profile or "").strip().lower()
    if not key:
        return ()
    if key == "upstox_standard":
        return upstox_standard_policies()
    if key == "alpaca_basic":
        return alpaca_market_data_policies("basic")
    if key == "alpaca_algo_trader_plus":
        return alpaca_market_data_policies("algo_trader_plus")
    raise SystemExit(f"unknown quota profile {profile!r}")


@dataclass
class Arm:
    name: str
    symbols: int = 100
    latency_ms: float = 100.0
    workers: int = 8
    rps: float = 50.0
    provider_rps: float = 0.0
    failures: dict[str, str] = field(default_factory=dict)
    trend: str = "diverse"
    note: str = ""
    # Schedule simulation (30 simulated minutes at a target cadence).
    cadence_s: float = 0.0
    quota_profile: str = ""
    market: str = "NSE"

    def gate_config(self, *, sequential: bool = False) -> FetchGateConfig:
        return FetchGateConfig(
            max_workers=1 if sequential else self.workers,
            max_requests_per_second=1000.0 if sequential else self.rps,
            burst=1000 if sequential else max(1, int(self.rps)),
            batch_timeout_seconds=120.0,
            quota_policies=quota_policies_for(self.quota_profile),
            quota_profile=self.quota_profile or "none",
        )


@dataclass
class Stack:
    engine: LiveMarketPaperEngine
    ensemble: StrategyEnsembleService
    symbols: list[str]
    nse_gate: ProviderFetchGate
    nasdaq_gate: ProviderFetchGate


def build_stack(arm: Arm, transport: FakeMarketData, *, sequential: bool = False) -> Stack:
    nse_gate = ProviderFetchGate(arm.gate_config(sequential=sequential))
    nasdaq_gate = ProviderFetchGate(arm.gate_config(sequential=sequential))
    engine = LiveMarketPaperEngine(
        ":memory:",
        nse_quote_provider=transport.quote,
        nasdaq_quote_provider=transport.quote,
        nse_bars_provider=transport.bars,
        nasdaq_bars_provider=transport.bars,
        nse_instrument_resolver=_resolver,
        nasdaq_instrument_resolver=_resolver,
        clock=lambda: SESSION_NOW,
        nse_fetch_gate=nse_gate,
        nasdaq_fetch_gate=nasdaq_gate,
    )
    ensemble = StrategyEnsembleService(
        ":memory:",
        paper_engine=engine,
        nse_quote_provider=transport.quote,
        nasdaq_quote_provider=transport.quote,
        nse_bars_provider=transport.bars,
        nasdaq_bars_provider=transport.bars,
        nse_instrument_resolver=_resolver,
        nasdaq_instrument_resolver=_resolver,
        clock=lambda: SESSION_NOW,
        nse_fetch_gate=nse_gate,
        nasdaq_fetch_gate=nasdaq_gate,
    )
    engine.ensemble_service = ensemble

    # Exactly `arm.symbols` selected symbols: drop the seeded demo watchlist.
    for item in engine.list_watchlists().get("NSE", []):
        engine.remove_watchlist_symbol("NSE", str(item.get("symbol") or ""))
    symbols = [f"SYN{idx:03d}" for idx in range(arm.symbols)]
    for sym in symbols:
        engine.add_watchlist_symbol("NSE", sym)
    engine.start_session(
        "NSE",
        strategy="parallel_ensemble",
        timeframe=BENCH_TF,
        symbols=symbols,
        execute_immediately=False,
    )
    return Stack(
        engine=engine, ensemble=ensemble, symbols=symbols,
        nse_gate=nse_gate, nasdaq_gate=nasdaq_gate,
    )


def _fingerprint(decisions: list[dict[str, Any]]) -> str:
    rows = sorted(
        (str(d.get("symbol")), str(d.get("final_action")), int(d.get("selected_qty") or 0))
        for d in decisions
    )
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------
def _scan_metrics(result: dict[str, Any], wall_ms: float, symbols: int) -> dict[str, Any]:
    counts = dict(result.get("counts") or {})
    fetch = dict(result.get("provider_fetch") or {})
    return {
        "wall_ms": round(wall_ms, 1),
        "per_symbol_ms": round(wall_ms / max(1, symbols), 3),
        "throughput_symbols_per_s": round(symbols / max(0.001, wall_ms / 1000.0), 2),
        "counts": counts,
        "provider_fetch": {
            key: fetch.get(key)
            for key in ("requests", "retries", "rate_limited_events", "cooldowns", "timeouts", "peak_inflight", "workers_used")
        },
        "stale_or_rejected": sorted(
            set(result.get("missing_data_symbols") or [])
            | set(result.get("failed_symbols") or [])
            | set(result.get("stale_data_symbols") or [])
        ),
    }


def measure_scan(arm: Arm, *, sequential: bool = False) -> dict[str, Any]:
    """Scan-only cycle: fetch + evaluate, zero orders (``analyze_market``)."""
    transport = FakeMarketData(
        latency_ms=arm.latency_ms,
        provider_rps=arm.provider_rps,
        failures=arm.failures,
        trend=arm.trend,
    )
    stack = build_stack(arm, transport, sequential=sequential)
    transport.reset_counters()
    stack.nse_gate.reset_stats()
    started = time.perf_counter()
    result = stack.engine.analyze_market("NSE", trigger="benchmark")
    wall_ms = (time.perf_counter() - started) * 1000.0
    metrics = _scan_metrics(result, wall_ms, arm.symbols)
    metrics["transport"] = {
        "requests": transport.calls,
        "quote_requests": transport.quote_calls,
        "bar_requests": transport.bar_calls,
        "rate_limited_429": transport.rate_limited,
        "fake_failures_served": transport.failures_served,
        "peak_inflight": transport.peak_inflight,
        "latency": transport.latency_summary(),
    }
    metrics["gate"] = stack.nse_gate.stats()
    metrics["fingerprint"] = hashlib.sha256(
        json.dumps(
            [(row["symbol"], row["status"], row.get("ensemble_final_action")) for row in result.get("symbols") or []],
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    stack.engine.close()
    return metrics


def measure_cycle(arm: Arm, *, sequential: bool = False, repeat: int = 2) -> dict[str, Any]:
    """Full cycle: fetch -> evaluate -> allocate -> plan/submit paper orders."""
    transport = FakeMarketData(
        latency_ms=arm.latency_ms,
        provider_rps=arm.provider_rps,
        failures=arm.failures,
        trend=arm.trend,
    )
    stack = build_stack(arm, transport, sequential=sequential)
    engine = stack.engine

    transport.reset_counters()
    stack.nse_gate.reset_stats()
    gc.collect()
    rss_before = _rss_mb()
    tracemalloc.start()
    started = time.perf_counter()
    cold = engine.sync_live_tick("NSE")
    cold_ms = (time.perf_counter() - started) * 1000.0
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = _rss_mb()

    cold_cycle = dict(cold.get("ensemble_cycle") or {})
    warm_ms = 0.0
    warm = engine.sync_live_tick("NSE")
    if repeat > 1:
        started = time.perf_counter()
        warm = engine.sync_live_tick("NSE")
        warm_ms = (time.perf_counter() - started) * 1000.0

    snapshot = engine.market_snapshot("NSE")
    limits = dict(snapshot.get("risk_limits") or {})
    decisions = list(cold_cycle.get("decisions") or [])
    buy_intents = [d for d in decisions if str(d.get("final_action")) in {"BUY", "SELL"} and int(d.get("selected_qty") or 0) > 0]
    blocked = [d for d in decisions if str(d.get("final_action")) == "NO_TRADE"]
    blocked_by_check: dict[str, int] = {}
    for dec in blocked:
        for check in dec.get("risk_checks") or []:
            if not check.get("passed"):
                key = str(check.get("check_id") or "unknown")
                blocked_by_check[key] = blocked_by_check.get(key, 0) + 1

    metrics = {
        "symbols": arm.symbols,
        "cold_wall_ms": round(cold_ms, 1),
        "warm_wall_ms": round(warm_ms, 1),
        "per_symbol_ms": round(cold_ms / max(1, arm.symbols), 3),
        "throughput_symbols_per_s": round(arm.symbols / max(0.001, cold_ms / 1000.0), 2),
        "decisions": {
            "evaluated": len(decisions),
            "candidates_buy_or_sell": len(buy_intents),
            "blocked_by_risk_or_signal": len(blocked),
            "blocked_by_check": dict(sorted(blocked_by_check.items())),
            "orders_submitted": int(cold_cycle.get("orders_submitted_simulated") or 0),
            "open_positions_after": len(snapshot.get("positions") or []),
            "working_orders_after": len(snapshot.get("orders") or []),
            "max_open_positions_limit": limits.get("max_open_positions"),
            "max_daily_orders_limit": limits.get("max_daily_orders"),
        },
        "provider": {
            "requests": transport.calls,
            "quote_requests": transport.quote_calls,
            "bar_requests": transport.bar_calls,
            "requests_per_symbol": round(transport.calls / max(1, arm.symbols), 3),
            "rate_limited_429": transport.rate_limited,
            "fake_failures_served": transport.failures_served,
            "peak_inflight": transport.peak_inflight,
            "latency": transport.latency_summary(),
            "gate": stack.nse_gate.stats(),
        },
        "memory": {
            "tracemalloc_peak_mb": round(peak_bytes / (1024 * 1024), 2),
            "rss_before_mb": round(rss_before, 1),
            "rss_after_mb": round(rss_after, 1),
            "rss_delta_mb": round(rss_after - rss_before, 1),
        },
        "status": {
            "rejected_instruments": dict(cold_cycle.get("rejected_instruments") or {}),
            "broker_order_api_calls": int(cold.get("ensemble_cycle", {}).get("broker_order_api_calls") or 0),
            "engine_broker_order_api_calls": 0,
        },
        "fingerprint": _fingerprint(decisions),
        "warm_fingerprint": _fingerprint(list(dict(warm.get("ensemble_cycle") or {}).get("decisions") or [])),
    }
    engine.close()
    return metrics


def _arm_report(arm: Arm, *, repeat: int = 2) -> dict[str, Any]:
    scan_seq = measure_scan(arm, sequential=True) if arm.name.startswith("sequential") else None
    scan = measure_scan(arm, sequential=arm.name.startswith("sequential"))
    cycle = measure_cycle(arm, sequential=arm.name.startswith("sequential"), repeat=repeat)
    return {
        "name": arm.name,
        "note": arm.note,
        "config": {
            "symbols": arm.symbols,
            "provider_latency_ms": arm.latency_ms,
            "worker_bound": 1 if arm.name.startswith("sequential") else arm.workers,
            "request_budget_rps": 1000.0 if arm.name.startswith("sequential") else arm.rps,
            "provider_enforced_rps": arm.provider_rps,
            "injected_failures": arm.failures,
            "synthetic_series": arm.trend,
        },
        "scan": scan,
        "cycle": cycle,
        "sequential_reference": scan_seq,
    }


# --------------------------------------------------------------------------
# Quota-bound 30-minute schedule simulation (virtual clock, no sleeping)
# --------------------------------------------------------------------------
class VirtualClock:
    """Monotonic virtual time: 30 simulated minutes cost seconds of real time."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, float(seconds))

    def advance_to(self, when: float) -> None:
        with self._lock:
            self._t = max(self._t, float(when))


def requests_per_category(symbols: int, market: str) -> dict[str, int]:
    """Requests a cycle of ``symbols`` puts into each quota category.

    A symbol costs one bar request and one quote request; when both endpoints
    map to the same documented category (Alpaca market data) a symbol therefore
    costs two requests from one pool.
    """
    endpoints = MARKET_ENDPOINT_CATEGORIES.get(market, MARKET_ENDPOINT_CATEGORIES["NSE"])
    counts: dict[str, int] = {}
    for category in endpoints.values():
        counts[category] = counts.get(category, 0) + symbols
    return counts


def sustainable_cadence(
    symbols: int, policies: tuple[QuotaPolicy, ...], *, market: str = "NSE"
) -> dict[str, Any]:
    """Minimum interval supported by the documented windows for ``symbols``.

    A cycle needs ``seconds * requests / limit`` seconds in every window, so the
    longest window usually binds (Upstox: the 2,000-per-30-minutes window).
    """
    per_cycle = requests_per_category(symbols, market)

    def _max_at(seconds_available: float, policy: QuotaPolicy, window) -> int:
        # Requests per symbol in this category: 1 for a per-endpoint pool
        # (Upstox quote / candle), 2 when both endpoints share one pool (Alpaca).
        factor = max(1, per_cycle.get(policy.category, symbols)) / max(1, symbols)
        return int((window.limit * seconds_available) / (window.seconds * factor))

    per_policy: dict[str, Any] = {}
    binding = 0.0
    symbols_at_15s: list[int] = []
    for policy in policies:
        rows = []
        count = max(1, per_cycle.get(policy.category, symbols))
        for window in policy.windows:
            need = (window.seconds * count) / max(1, window.limit)
            rows.append(
                {
                    "window": window.name,
                    "seconds": window.seconds,
                    "limit": window.limit,
                    "requests_per_cycle": count,
                    "min_interval_s": round(need, 3),
                }
            )
            binding = max(binding, need)
            symbols_at_15s.append(_max_at(15.0, policy, window))
        per_policy[policy.category] = {"windows": rows}
    return {
        "symbols": symbols,
        "market": market,
        "requests_per_cycle_per_category": per_cycle,
        "min_interval_seconds": round(binding, 3),
        "max_symbols_at_15s": min(symbols_at_15s) if symbols_at_15s else None,
        "max_symbols_at_60s": (
            min(int(_max_at(60.0, p, w)) for p in policies for w in p.windows) if policies else None
        ),
        "per_category": per_policy,
    }


def simulate_schedule(arm: Arm, *, minutes: float = 30.0) -> dict[str, Any]:
    """Run ``minutes`` of a virtual schedule at ``arm.cadence_s`` and audit quotas.

    One cycle issues one bar request and one quote request per symbol, exactly as
    ``analyze_market`` does, through the same :class:`ProviderFetchGate` and the
    same fake transport. The clock is virtual (provider latency and quota waits
    advance it), so a 30-minute schedule completes in seconds while the quota
    windows, AIMD budget and cooldowns behave exactly as they do in production.
    A cycle starts on the cadence grid, or immediately when the previous cycle
    overran it - that overrun is what "cannot hold this cadence" looks like.
    """
    cadence = float(arm.cadence_s or 15.0)
    vclock = VirtualClock()
    transport = FakeMarketData(
        latency_ms=arm.latency_ms,
        provider_rps=arm.provider_rps,
        failures=arm.failures,
        trend=arm.trend,
        sleep_fn=vclock.sleep,
        clock_fn=vclock.now,
    )
    gate = ProviderFetchGate(
        FetchGateConfig(
            max_workers=arm.workers,
            max_requests_per_second=arm.rps,
            burst=max(1, int(arm.rps)),
            batch_timeout_seconds=120.0,
            quota_policies=quota_policies_for(arm.quota_profile),
            quota_profile=arm.quota_profile or "none",
        ),
        clock=vclock.now,
        sleep=vclock.sleep,
    )
    cats = MARKET_ENDPOINT_CATEGORIES.get(arm.market, MARKET_ENDPOINT_CATEGORIES["NSE"])
    bars = gate.guarded(cats["bars"], lambda sym: transport.bars(sym, BENCH_TF))
    quotes = gate.guarded(cats["quote"], transport.quote)
    symbols = [f"SYN{i:03d}" for i in range(arm.symbols)]

    def _one_symbol(sym: str) -> int:
        rows = bars(sym)
        quotes(sym)
        return len(rows)

    def _one_cycle() -> dict[str, Any]:
        started = vclock.now()
        outcomes = gate.map(symbols, _one_symbol, paced=False)
        finished = vclock.now()
        ok = sum(1 for o in outcomes.values() if o.ok)
        codes: dict[str, int] = {}
        for o in outcomes.values():
            if not o.ok:
                codes[o.error_code] = codes.get(o.error_code, 0) + 1
        return {
            "start_s": round(started, 3),
            "finish_s": round(finished, 3),
            "latency_s": round(finished - started, 3),
            "symbols_ok": ok,
            "symbols_missing": len(outcomes) - ok,
            "symbols_deferred": codes.get("QUOTA_DEFERRED", 0),
            "fully_deferred": ok == 0 and codes.get("QUOTA_DEFERRED", 0) > 0,
            "error_codes": codes,
        }

    horizon = float(minutes) * 60.0
    cycles: list[dict[str, Any]] = []
    next_start = 0.0
    slipped = 0
    while next_start < horizon:
        vclock.advance_to(next_start)
        cycle = _one_cycle()
        cycles.append(cycle)
        planned_next = next_start + cadence
        if cycle["finish_s"] > planned_next:
            slipped += 1
            next_start = cycle["finish_s"]
        else:
            next_start = planned_next

    stats = gate.stats()
    quota = stats.get("quota") or {}
    # Cycles whose scheduled start falls inside the horizon (ceil, so a 10-minute
    # run at a 90 s cadence schedules 7, not 6).
    requested_cycles = int(math.ceil(horizon / cadence))
    cycles_with_data = sum(1 for c in cycles if c["symbols_ok"] > 0)
    cycles_fully_deferred = sum(1 for c in cycles if c["fully_deferred"])
    # Deferred cycles return almost instantly by design; latency is reported for
    # the cycles that actually fetched data.
    latencies = sorted(c["latency_s"] for c in cycles if c["symbols_ok"] > 0) or [0.0]

    def _pct(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
        return round(values[idx], 3)

    per_category: dict[str, Any] = {}
    for category, cat in (quota.get("categories") or {}).items():
        windows = cat.get("windows") or {}
        per_category[category] = {
            "label": cat.get("label"),
            "requests": cat.get("requests"),
            "requests_per_second": round(cat.get("requests", 0) / horizon, 4),
            "requests_per_minute": round(cat.get("requests", 0) / (horizon / 60.0), 2),
            "horizon_seconds": horizon,
            "peak_in_window": {name: w["peak"] for name, w in windows.items()},
            "limit_in_window": {name: w["limit"] for name, w in windows.items()},
            "window_blocks": {name: w["blocks"] for name, w in windows.items()},
            "quota_waits": cat.get("quota_waits"),
            "quota_wait_seconds": cat.get("quota_wait_seconds"),
            "rate_limited_429": cat.get("rate_limited_events"),
            "within_documented_limits": all(
                w["peak"] <= w["limit"] for w in windows.values()
            ),
        }

    total_symbols = len(cycles) * arm.symbols
    deferred_symbols = sum(c["symbols_deferred"] for c in cycles)
    missing_symbols = sum(c["symbols_missing"] for c in cycles) - deferred_symbols
    return {
        "arm": arm.name,
        "note": arm.note,
        "market": arm.market,
        "quota_profile": arm.quota_profile or "none",
        "quota_policies": [p.to_dict() for p in quota_policies_for(arm.quota_profile)],
        "config": {
            "symbols": arm.symbols,
            "requested_cadence_s": cadence,
            "minutes": minutes,
            "worker_bound": arm.workers,
            "request_budget_rps": arm.rps,
            "provider_latency_ms": arm.latency_ms,
            "provider_enforced_rps": arm.provider_rps,
            "time_source": "virtual clock (latency + quota waits advance it)",
        },
        "cycles": {
            "requested": requested_cycles,
            "completed": len(cycles),
            # "Held" means every scheduled cycle actually produced data; a cycle
            # that was deferred for quota did not hold the cadence.
            "cadence_held": cycles_with_data == requested_cycles and slipped == 0,
            "cycles_with_data": cycles_with_data,
            "cycles_fully_deferred": cycles_fully_deferred,
            "slipped_past_cadence": slipped,
            "achieved_mean_interval_s": round(
                (cycles[-1]["start_s"] - cycles[0]["start_s"]) / (len(cycles) - 1), 3
            )
            if len(cycles) > 1
            else None,
            "min_interval_s": round(
                min(b["start_s"] - a["start_s"] for a, b in zip(cycles, cycles[1:])), 3
            )
            if len(cycles) > 1
            else None,
            "cycle_latency_s": {
                "p50": _pct(latencies, 0.50),
                "p95": _pct(latencies, 0.95),
                "max": _pct(latencies, 1.0),
            },
            "symbols_requested": total_symbols,
            "symbols_failed": missing_symbols,
            "symbols_deferred": deferred_symbols,
            "symbols_missing_total": missing_symbols + deferred_symbols,
            "symbols_failed_pct": round(100.0 * missing_symbols / max(1, total_symbols), 3),
        },
        "per_endpoint": per_category,
        "gate": {
            key: stats.get(key)
            for key in (
                "requests",
                "retries",
                "rate_limited_events",
                "cooldowns",
                "timeouts",
                "budget_reductions",
                "budget_increases",
                "effective_requests_per_second",
                "quota_waits",
                "quota_wait_seconds",
                "deferred",
                "peak_inflight",
            )
        },
        "provider_transport": {
            "requests": transport.calls,
            "quote_requests": transport.quote_calls,
            "bar_requests": transport.bar_calls,
            "rate_limited_429": transport.rate_limited,
            "peak_inflight": transport.peak_inflight,
        },
        "documented_cadence": sustainable_cadence(
            arm.symbols, quota_policies_for(arm.quota_profile), market=arm.market
        ),
        "broker_order_api_calls": 0,
    }


def schedule_arms(symbols: int) -> list[Arm]:
    """30-minute schedules: the intended cadence, below it, and the throttled case."""
    return [
        Arm("schedule-100syms-every-15s", symbols=symbols, latency_ms=25.0, workers=8, rps=50.0,
            cadence_s=15.0, quota_profile="upstox_standard",
            note="intended cadence: 100 NSE symbols every 15 s against the documented Upstox windows"),
        Arm("schedule-100syms-every-60s", symbols=symbols, latency_ms=25.0, workers=8, rps=50.0,
            cadence_s=60.0, quota_profile="upstox_standard",
            note="60 s cadence: still above the 30-minute window's sustainable rate"),
        Arm("schedule-100syms-every-90s", symbols=symbols, latency_ms=25.0, workers=8, rps=50.0,
            cadence_s=90.0, quota_profile="upstox_standard",
            note="90 s cadence: the interval the 2,000/30 min window actually supports for 100 symbols"),
        Arm("schedule-100syms-every-15s-throttled", symbols=symbols, latency_ms=25.0, workers=8, rps=50.0,
            provider_rps=20.0, cadence_s=15.0, quota_profile="upstox_standard",
            note="provider additionally throttles above 20 req/s: AIMD must not bypass the windows"),
        Arm("schedule-16syms-every-15s", symbols=16, latency_ms=25.0, workers=8, rps=50.0,
            cadence_s=15.0, quota_profile="upstox_standard",
            note="the symbol count a 15 s cadence does support on Upstox standard APIs"),
        Arm("schedule-100syms-every-15s-alpaca-basic", symbols=symbols, latency_ms=25.0, workers=8, rps=50.0,
            cadence_s=15.0, quota_profile="alpaca_basic", market="NASDAQ",
            note="100 NASDAQ symbols every 15 s on the documented Alpaca Basic 200/min limit"),
    ]


def default_arms(symbols: int, *, quick: bool = False) -> list[Arm]:
    if quick:
        return [
            Arm("bounded-8w-50rps-25ms", symbols=min(symbols, 100), latency_ms=25.0, workers=8, rps=50.0,
                quota_profile="upstox_standard",
                note="bounded fan-out, local fake latency, documented Upstox windows"),
            Arm("partial-failures", symbols=min(symbols, 100), latency_ms=10.0, workers=8, rps=50.0,
                failures={f"SYN{i:03d}": mode for i, mode in zip(range(0, 12, 2), ["error", "error", "timeout", "empty_bars", "stale_quote", "rate_limit"])},
                note="6 of N symbols fail; the rest must be identical to the clean run"),
        ]
    return [
        Arm("sequential-reference-100ms", symbols=symbols, latency_ms=100.0, workers=1, rps=1000.0, quota_profile="upstox_standard",
            note="one worker, no pacing: the pre-change fan-out shape (sequential loops)"),
        Arm("bounded-8w-50rps-100ms", symbols=symbols, latency_ms=100.0, workers=8, rps=50.0, quota_profile="upstox_standard",
            note="bounded fan-out + request budget at a realistic round trip"),
        Arm("bounded-8w-50rps-250ms", symbols=symbols, latency_ms=250.0, workers=8, rps=50.0, quota_profile="upstox_standard",
            note="slow provider: latency-bound, budget has headroom"),
        Arm("provider-429-at-20rps", symbols=symbols, latency_ms=25.0, workers=8, rps=50.0, provider_rps=20.0, quota_profile="upstox_standard",
            note="provider throttles above 20 req/s: the gate must back off centrally, not per worker"),
        Arm("correlated-cluster-100syms", symbols=symbols, latency_ms=10.0, workers=8, rps=50.0, trend="up", quota_profile="upstox_standard",
            note="100 perfectly correlated names: the portfolio correlation gate must refuse to hold them all"),
        Arm("partial-failures", symbols=symbols, latency_ms=50.0, workers=8, rps=50.0, quota_profile="upstox_standard",
            failures={
                **{f"SYN{i:03d}": "error" for i in range(0, 10, 2)},
                **{f"SYN{i:03d}": "timeout" for i in range(20, 26, 2)},
                **{f"SYN{i:03d}": "empty_bars" for i in range(40, 44)},
                **{f"SYN{i:03d}": "stale_quote" for i in range(60, 66, 2)},
            },
            note="5 error + 3 timeout + 4 empty + 3 stale symbols; every other symbol must match the clean run"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", type=int, default=100)
    parser.add_argument("--json", type=str, default="")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--arms", type=str, default="", help="comma-separated arm names to run")
    parser.add_argument(
        "--schedule",
        type=str,
        default="auto",
        choices=("auto", "on", "off"),
        help="30-minute quota-bound schedule simulation (auto: on unless --quick)",
    )
    parser.add_argument("--schedule-minutes", type=float, default=30.0)
    args = parser.parse_args(argv)

    symbols = max(1, min(500, args.symbols))
    arms = default_arms(symbols, quick=args.quick)
    if args.arms:
        wanted = {a.strip() for a in args.arms.split(",") if a.strip()}
        arms = [a for a in arms if a.name in wanted]

    report: dict[str, Any] = {
        "schema": "axiom.market_scan_capacity.v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "method": {
            "paths": [
                "LiveMarketPaperEngine.analyze_market (scan: fetch + ensemble preview)",
                "LiveMarketPaperEngine.sync_live_tick (cycle: fetch -> evaluate -> allocate -> paper order plan)",
                "StrategyEnsembleService.evaluate_watchlist_cycle",
                "PortfolioRiskAllocator.allocate_symbol",
            ],
            "transport": "local fake provider (deterministic synthetic OHLCV/quotes, injectable latency/failures)",
            "network_calls": 0,
            "broker_order_api_calls": 0,
            "execution_boundary_constructed": False,
            "session_clock": SESSION_NOW.isoformat(),
            "timeframe": BENCH_TF,
            "bars_per_symbol": BARS_PER_SYMBOL,
            "ensemble_variants_per_symbol": 24,
        },
        "limits": {
            "watchlist_symbols": "unbounded by code (SQLite rows, no cap in add_watchlist_symbol)",
            "symbols_per_cycle": "all selected symbols; no per-cycle cap, only provider budget and deadline",
            "open_positions_cap": 8,
            "daily_orders_cap": 40,
            "note": "selecting 100 symbols is not permission to hold or trade 100 instruments",
        },
        "arms": [],
    }

    for arm in arms:
        print(f"running arm {arm.name} ({arm.symbols} symbols, {arm.latency_ms:.0f} ms, "
              f"{'sequential' if arm.name.startswith('sequential') else f'{arm.workers}w/{arm.rps:g}rps'}) ...",
              file=sys.stderr, flush=True)
        report["arms"].append(_arm_report(arm))

    # Determinism: a second, independent stack with the same arm must decide identically.
    determinism_arm = arms[-1]
    second = measure_cycle(determinism_arm)
    first = next(a for a in report["arms"] if a["name"] == determinism_arm.name)
    report["determinism"] = {
        "arm": determinism_arm.name,
        "first_run_fingerprint": first["cycle"]["fingerprint"],
        "second_run_fingerprint": second["fingerprint"],
        "identical": first["cycle"]["fingerprint"] == second["fingerprint"],
    }

    if not args.quick:
        clean = next((a for a in report["arms"] if a["name"].startswith("bounded-8w-50rps-100ms")), None)
        noisy = next((a for a in report["arms"] if a["name"] == "partial-failures"), None)
        if clean and noisy:
            clean_scan = clean["scan"]
            noisy_scan = noisy["scan"]
            injected = set(noisy["config"]["injected_failures"])
            healthy = [s for s in noisy_scan["stale_or_rejected"] if s not in injected]
            report["failure_isolation"] = {
                "injected_symbols": sorted(injected),
                "injected_count": len(injected),
                "healthy_symbols_flagged": healthy,
                "healthy_symbols_flagged_count": len(healthy),
                "healthy_scan_succeeded": noisy_scan["counts"].get("succeeded"),
                "clean_scan_succeeded": clean_scan["counts"].get("succeeded"),
                "expected_healthy": clean_scan["counts"].get("succeeded", 0) - len(injected),
                "isolated": not healthy,
            }

    run_schedule = args.schedule == "on" or (args.schedule == "auto" and not args.quick)
    if run_schedule:
        report["schedule_simulation"] = []
        for sarm in schedule_arms(symbols):
            print(
                f"simulating {sarm.name}: {sarm.symbols} symbols every {sarm.cadence_s:g}s "
                f"over {args.schedule_minutes:g} virtual minutes "
                f"({sarm.quota_profile or 'no quota'} ...)",
                file=sys.stderr,
                flush=True,
            )
            report["schedule_simulation"].append(
                simulate_schedule(sarm, minutes=args.schedule_minutes)
            )
        report["documented_quotas"] = {
            policy.category: policy.to_dict()
            for profile in ("upstox_standard", "alpaca_basic", "alpaca_algo_trader_plus")
            for policy in quota_policies_for(profile)
        }
        report["sustainable_cadence"] = {
            "upstox_standard": sustainable_cadence(
                symbols, quota_policies_for("upstox_standard"), market="NSE"
            ),
            "alpaca_basic": sustainable_cadence(
                symbols, quota_policies_for("alpaca_basic"), market="NASDAQ"
            ),
            "alpaca_algo_trader_plus": sustainable_cadence(
                symbols, quota_policies_for("alpaca_algo_trader_plus"), market="NASDAQ"
            ),
        }

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
