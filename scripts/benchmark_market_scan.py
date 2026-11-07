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
from app.markets.fetch_gate import FetchGateConfig, ProviderFetchGate  # noqa: E402
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
    ) -> None:
        self.latency = max(0.0, float(latency_ms)) / 1000.0
        self.provider_rps = float(provider_rps or 0.0)
        self.failures = dict(failures or {})
        self.trend = trend
        self._lock = threading.Lock()
        self._window_start = time.monotonic()
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
                now = time.monotonic()
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
            self.latencies_ms.append((time.monotonic() - started) * 1000.0)

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
        started = time.monotonic()
        try:
            if self.latency:
                time.sleep(self.latency)
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
        started = time.monotonic()
        try:
            if self.latency:
                time.sleep(self.latency)
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

    def gate_config(self, *, sequential: bool = False) -> FetchGateConfig:
        return FetchGateConfig(
            max_workers=1 if sequential else self.workers,
            max_requests_per_second=1000.0 if sequential else self.rps,
            burst=1000 if sequential else max(1, int(self.rps)),
            batch_timeout_seconds=120.0,
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


def default_arms(symbols: int, *, quick: bool = False) -> list[Arm]:
    if quick:
        return [
            Arm("bounded-8w-50rps-25ms", symbols=min(symbols, 100), latency_ms=25.0, workers=8, rps=50.0,
                note="bounded fan-out, local fake latency"),
            Arm("partial-failures", symbols=min(symbols, 100), latency_ms=10.0, workers=8, rps=50.0,
                failures={f"SYN{i:03d}": mode for i, mode in zip(range(0, 12, 2), ["error", "error", "timeout", "empty_bars", "stale_quote", "rate_limit"])},
                note="6 of N symbols fail; the rest must be identical to the clean run"),
        ]
    return [
        Arm("sequential-reference-100ms", symbols=symbols, latency_ms=100.0, workers=1, rps=1000.0,
            note="one worker, no pacing: the pre-change fan-out shape (sequential loops)"),
        Arm("bounded-8w-50rps-100ms", symbols=symbols, latency_ms=100.0, workers=8, rps=50.0,
            note="bounded fan-out + request budget at a realistic round trip"),
        Arm("bounded-8w-50rps-250ms", symbols=symbols, latency_ms=250.0, workers=8, rps=50.0,
            note="slow provider: latency-bound, budget has headroom"),
        Arm("provider-429-at-20rps", symbols=symbols, latency_ms=25.0, workers=8, rps=50.0, provider_rps=20.0,
            note="provider throttles above 20 req/s: the gate must back off centrally, not per worker"),
        Arm("correlated-cluster-100syms", symbols=symbols, latency_ms=10.0, workers=8, rps=50.0, trend="up",
            note="100 perfectly correlated names: the portfolio correlation gate must refuse to hold them all"),
        Arm("partial-failures", symbols=symbols, latency_ms=50.0, workers=8, rps=50.0,
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
    args = parser.parse_args(argv)

    symbols = max(1, min(500, args.symbols))
    arms = default_arms(symbols, quick=args.quick)
    if args.arms:
        wanted = {a.strip() for a in args.arms.split(",") if a.strip()}
        arms = [a for a in arms if a.name in wanted]

    report: dict[str, Any] = {
        "schema": "axiom.market_scan_capacity.v1",
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

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
