"""Capacity contract for scanning a 100-symbol watchlist in one cycle.

Two layers are covered:

* ``app.markets.fetch_gate`` — the bounded, rate-limit-aware fan-out (worker
  bound, request budget, shared cooldown, batch deadline, per-symbol isolation).
* the real scan/cycle paths (``LiveMarketPaperEngine.analyze_market`` and
  ``sync_live_tick`` → ``evaluate_watchlist_cycle`` → ``PortfolioRiskAllocator``)
  driven by the offline benchmark harness with fake transports.

Everything here is local: no provider request leaves the process and the live
execution boundary (``app.markets.live_execution``) is never constructed, so no
broker order endpoint can be reached.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import threading
import time

import pytest

from app.markets.fetch_gate import FetchGateConfig, ProviderFetchGate

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_benchmark():
    spec = importlib.util.spec_from_file_location(
        "axiom_benchmark_market_scan", ROOT / "scripts" / "benchmark_market_scan.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules, which needs the
    # module registered before execution.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BENCH = _load_benchmark()


class _FakeClock:
    """Deterministic clock + sleeper: pacing is asserted without real waiting."""

    def __init__(self) -> None:
        self.now_value = 1_000.0
        self.slept = 0.0

    def now(self) -> float:
        return self.now_value

    def sleep(self, seconds: float) -> None:
        self.slept += float(seconds)
        self.now_value += float(seconds)


# --------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------- #
def test_gate_bounds_concurrency() -> None:
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def fetch(symbol: str) -> str:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.01)
        with lock:
            in_flight -= 1
        return symbol

    gate = ProviderFetchGate(
        FetchGateConfig(max_workers=4, max_requests_per_second=10_000, burst=100)
    )
    outcomes = gate.map([f"S{i:02d}" for i in range(24)], fetch)
    assert len(outcomes) == 24
    assert all(o.ok for o in outcomes.values())
    assert peak <= 4, f"worker bound exceeded: {peak}"
    assert gate.stats()["peak_inflight"] <= 4


def test_gate_paces_requests_to_the_budget() -> None:
    clock = _FakeClock()
    gate = ProviderFetchGate(
        FetchGateConfig(max_workers=1, max_requests_per_second=10.0, burst=1),
        clock=clock.now,
        sleep=clock.sleep,
    )
    gate.map([f"S{i:02d}" for i in range(11)], lambda symbol: symbol)
    # 10 requests/second with one token of burst: the 11th call waits ~1 s.
    assert 0.85 <= clock.slept <= 1.3, clock.slept


def test_one_bad_symbol_never_affects_the_others() -> None:
    def fetch(symbol: str):
        if symbol == "BAD":
            raise ValueError("synthetic symbol failure")
        return f"value-{symbol}"

    gate = ProviderFetchGate(FetchGateConfig(max_workers=4, max_requests_per_second=1000, burst=100))
    outcomes = gate.map(["A", "BAD", "B", "C"], fetch)
    assert outcomes["BAD"].ok is False
    assert outcomes["BAD"].error_code == "PROVIDER_ERROR"
    assert [outcomes[key].value for key in ("A", "B", "C")] == ["value-A", "value-B", "value-C"]
    # Nothing is dropped or reordered: the caller can iterate its own symbol list.
    assert list(outcomes) == ["A", "BAD", "B", "C"]


def test_rate_limit_backs_off_then_reports_the_symbol() -> None:
    clock = _FakeClock()
    calls = {"n": 0}

    def fetch(symbol: str):
        calls["n"] += 1
        raise RuntimeError("HTTP 429 Too Many Requests")

    gate = ProviderFetchGate(
        FetchGateConfig(
            max_workers=1,
            max_requests_per_second=50.0,
            burst=50,
            cooldown_seconds=0.5,
            max_cooldown_seconds=2.0,
            retries_after_rate_limit=1,
        ),
        clock=clock.now,
        sleep=clock.sleep,
    )
    outcomes = gate.map(["THROTTLED"], fetch)
    stats = gate.stats()
    assert outcomes["THROTTLED"].error_code == "RATE_LIMITED"
    assert outcomes["THROTTLED"].attempts == 2, "one bounded retry after the cooldown"
    assert calls["n"] == 2
    # Two throttles (initial + retry) ⇒ two windows, the second with doubled backoff.
    assert stats["cooldowns"] == 2
    # The first throttle is waited out before the retry; the gate does not sleep
    # again once the retry budget is exhausted.
    assert clock.slept >= 0.45, "cooldown must actually wait"
    assert stats["rate_limited_events"] == 2
    assert stats["budget_reductions"] >= 1
    assert stats["effective_requests_per_second"] < stats["config"]["max_requests_per_second"]


def test_throttle_only_requests_recover_on_retry() -> None:
    seen = {"n": 0}

    def fetch(symbol: str):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return "ok"

    gate = ProviderFetchGate(
        FetchGateConfig(max_workers=1, max_requests_per_second=50.0, burst=50, cooldown_seconds=0.01)
    )
    outcomes = gate.map(["S1"], fetch)
    assert outcomes["S1"].ok and outcomes["S1"].value == "ok"
    assert outcomes["S1"].attempts == 2
    assert gate.stats()["retries"] == 1


def test_hung_provider_hits_the_batch_deadline() -> None:
    release = threading.Event()

    def fetch(symbol: str):
        if symbol == "HANG":
            release.wait(5.0)
            return "late"
        return symbol

    gate = ProviderFetchGate(
        FetchGateConfig(max_workers=2, max_requests_per_second=1000, burst=100, batch_timeout_seconds=1.0)
    )
    started = time.perf_counter()
    outcomes = gate.map(["HANG", "OK"], fetch)
    elapsed = time.perf_counter() - started
    release.set()
    assert elapsed < 4.0, "a hung provider call must not stall the cycle"
    assert outcomes["OK"].ok
    assert outcomes["HANG"].ok is False
    assert outcomes["HANG"].error_code == "FETCH_TIMEOUT"


def test_empty_input_returns_empty_mapping() -> None:
    gate = ProviderFetchGate(FetchGateConfig())
    assert gate.map([], lambda symbol: symbol) == {}


# --------------------------------------------------------------------------- #
# Real scan path, 100 symbols, fake transports
# --------------------------------------------------------------------------- #
def _arm(**overrides):
    base = dict(
        name="pytest-100",
        symbols=100,
        latency_ms=1.0,
        workers=8,
        rps=200.0,
        trend="diverse",
    )
    base.update(overrides)
    return BENCH.Arm(**base)


def test_hundred_symbol_scan_uses_two_requests_per_symbol() -> None:
    metrics = BENCH.measure_scan(_arm())
    counts = metrics["counts"]
    assert counts["selected_symbols"] == 100
    assert counts["succeeded"] == 100, counts
    assert counts["failed"] == 0 and counts["stale_data"] == 0
    assert metrics["transport"]["requests"] == 200, "one bar series + one quote per symbol"
    assert metrics["transport"]["peak_inflight"] <= 8
    assert metrics["gate"]["workers_used"] == 8


def test_hundred_symbol_scan_is_faster_than_sequential() -> None:
    arm = _arm(latency_ms=20.0)
    sequential = BENCH.measure_scan(arm, sequential=True)
    bounded = BENCH.measure_scan(arm, sequential=False)
    assert sequential["counts"]["succeeded"] == bounded["counts"]["succeeded"] == 100
    # Same work, same results, materially less wall clock.
    assert bounded["wall_ms"] < sequential["wall_ms"] / 2.0, (
        sequential["wall_ms"],
        bounded["wall_ms"],
    )
    assert bounded["fingerprint"] == sequential["fingerprint"], "results must not depend on fan-out"


def test_full_cycle_respects_position_exposure_and_order_caps() -> None:
    metrics = BENCH.measure_cycle(_arm(latency_ms=1.0, rps=500.0), repeat=1)
    decisions = metrics["decisions"]
    assert decisions["evaluated"] == 100
    # The portfolio layer, not the watchlist size, decides how many can be held.
    assert decisions["open_positions_after"] <= 8
    assert decisions["orders_submitted"] <= 8
    assert decisions["orders_submitted"] <= int(decisions["max_daily_orders_limit"])
    assert decisions["candidates_buy_or_sell"] >= decisions["orders_submitted"]
    assert metrics["status"]["broker_order_api_calls"] == 0


def test_cycle_is_deterministic_across_independent_stacks() -> None:
    first = BENCH.measure_cycle(_arm(latency_ms=1.0, rps=500.0), repeat=1)
    second = BENCH.measure_cycle(_arm(latency_ms=1.0, rps=500.0), repeat=1)
    assert first["fingerprint"] == second["fingerprint"]


def test_partial_provider_failures_are_isolated() -> None:
    clean = BENCH.measure_scan(_arm(latency_ms=1.0))
    failures = {
        "SYN000": "error",
        "SYN002": "timeout",
        "SYN004": "empty_bars",
        "SYN006": "stale_quote",
    }
    noisy = BENCH.measure_scan(_arm(latency_ms=1.0, failures=failures))
    flagged = set(noisy["stale_or_rejected"])
    assert set(failures).issubset(flagged), flagged
    healthy_flagged = flagged - set(failures)
    assert not healthy_flagged, f"unaffected symbols were corrupted: {sorted(healthy_flagged)}"
    assert noisy["counts"]["succeeded"] == clean["counts"]["succeeded"] - len(failures)
    # Identical per-symbol outcome for every healthy symbol.
    assert noisy["fingerprint"] != clean["fingerprint"]  # the injected failures change the set
    assert noisy["gate"]["requests"] > 0


def test_correlated_cluster_is_refused_by_the_risk_layer() -> None:
    metrics = BENCH.measure_cycle(_arm(latency_ms=1.0, rps=500.0, trend="up"), repeat=1)
    decisions = metrics["decisions"]
    assert decisions["evaluated"] == 100
    # 100 perfectly correlated names must not become 100 positions.
    assert decisions["open_positions_after"] <= 2, decisions
    assert decisions["blocked_by_check"].get("concentration_and_correlation", 0) >= 90


def test_provider_gate_never_calls_a_broker_order_endpoint() -> None:
    from app.markets.live_paper import LiveMarketPaperEngine

    assert LiveMarketPaperEngine.broker_order_endpoints_reachable is False
    metrics = BENCH.measure_cycle(_arm(latency_ms=1.0, rps=500.0), repeat=1)
    payload = json.dumps(metrics)
    assert "broker_order_api_calls\": 0" in payload
