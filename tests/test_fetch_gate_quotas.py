"""Provider quota compliance: rolling windows, per-endpoint pools, 429 behaviour.

The gate must respect every *documented* provider window, not just requests per
second. These tests pin the numbers to the providers' own documentation and prove
the enforcement: the per-second, per-minute and 30-minute windows all bind, the
quote and candle endpoints have separate pools (Upstox documents per-API limits),
a 429 lowers the per-second budget without widening or resetting a window, and a
throttled symbol cannot consume another symbol's quota.

Everything here is local: a virtual clock, fake transports, no sockets and no
broker order calls.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading

import pytest

from app.markets.fetch_gate import (
    DOCUMENTED_QUOTAS,
    MARKET_ENDPOINT_CATEGORIES,
    FetchGateConfig,
    ProviderFetchGate,
    QuotaWindow,
    alpaca_market_data_policies,
    upstox_standard_policies,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_benchmark():
    spec = importlib.util.spec_from_file_location(
        "axiom_benchmark_schedule", ROOT / "scripts" / "benchmark_market_scan.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BENCH = _load_benchmark()


class VirtualClock:
    """Deterministic clock: sleeping advances time instantly."""

    def __init__(self) -> None:
        self._t = 0.0
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


def _gate(policies, *, rps: float = 200.0, burst: int = 200) -> tuple[ProviderFetchGate, VirtualClock]:
    clock = VirtualClock()
    gate = ProviderFetchGate(
        FetchGateConfig(
            max_workers=4,
            max_requests_per_second=rps,
            burst=burst,
            quota_policies=policies,
        ),
        clock=clock.now,
        sleep=clock.sleep,
    )
    return gate, clock


# --------------------------------------------------------------------------- #
# The numbers come from the providers' documentation
# --------------------------------------------------------------------------- #
def test_upstox_standard_windows_match_the_documented_table() -> None:
    policies = upstox_standard_policies()
    assert {p.category for p in policies} == {
        "upstox.market_quote",
        "upstox.historical_candle",
    }
    for policy in policies:
        windows = {w.name: (w.seconds, w.limit) for w in policy.windows}
        assert windows == {"1s": (1.0, 50), "1m": (60.0, 500), "30m": (1800.0, 2000)}
        assert "per-API, per-user" in policy.documented_as
        assert policy.source_url.startswith("https://upstox.com/developer/api-documentation/rate-limiting")
    # Two endpoints, two pools: the documentation says per-API, so the quote and
    # candle pools are never merged into one shared budget.
    assert policies[0].category != policies[1].category


def test_alpaca_market_data_matches_the_documented_plan_limits() -> None:
    basic = alpaca_market_data_policies("basic")
    plus = alpaca_market_data_policies("algo_trader_plus")
    assert [w.limit for w in basic[0].windows] == [200]
    assert [w.seconds for w in basic[0].windows] == [60.0]
    assert [w.limit for w in plus[0].windows] == [10_000]
    # No per-second or 30-minute window is invented for a product that does not
    # document one.
    assert {w.name for w in basic[0].windows} == {"1m"}
    # Both Alpaca endpoints share the one documented per-plan figure.
    nasdaq = MARKET_ENDPOINT_CATEGORIES["NASDAQ"]
    assert nasdaq["quote"] == nasdaq["bars"] == basic[0].category
    with pytest.raises(ValueError):
        alpaca_market_data_policies("imagine_a_bigger_plan")


def test_documented_quota_registry_is_exposed_for_audit() -> None:
    assert set(DOCUMENTED_QUOTAS) == {
        "upstox_standard",
        "alpaca_basic",
        "alpaca_algo_trader_plus",
    }
    for policies in DOCUMENTED_QUOTAS.values():
        for policy in policies:
            payload = policy.to_dict()
            assert payload["source_url"] and payload["documented_as"] and payload["checked_on"]


# --------------------------------------------------------------------------- #
# Every window binds
# --------------------------------------------------------------------------- #
def test_minute_window_binds_before_the_per_second_budget_would() -> None:
    gate, clock = _gate(upstox_standard_policies())
    for _ in range(520):  # > 500/minute, far below any per-second limit
        gate.admit("upstox.market_quote")
    assert clock.now() >= 60.0, "the 500/minute window must have blocked"
    usage = gate.quota_usage("upstox.market_quote")
    assert usage["1m"] <= 500
    assert usage["30m"] <= 2000
    report = gate.quota_report()["categories"]["upstox.market_quote"]["windows"]
    assert report["1m"]["peak"] <= 500
    assert report["1m"]["blocks"] >= 1


def test_thirty_minute_window_binds_even_with_an_unlimited_per_second_budget() -> None:
    gate, clock = _gate(upstox_standard_policies(), rps=200.0, burst=200)
    for _ in range(2100):
        gate.admit("upstox.historical_candle")
    # 2,100 requests cannot fit in less than the 30-minute window's rate.
    assert clock.now() >= 1799.0
    windows = gate.quota_report()["categories"]["upstox.historical_candle"]["windows"]
    assert windows["30m"]["peak"] <= 2000
    assert windows["1s"]["peak"] <= 50
    assert windows["1m"]["peak"] <= 500


def test_per_endpoint_pools_are_independent() -> None:
    gate, clock = _gate(upstox_standard_policies())
    for _ in range(500):
        gate.admit("upstox.market_quote")
    # The quote pool is exhausted for this minute …
    before = clock.now()
    gate.admit("upstox.market_quote")
    assert clock.now() >= 60.0 and clock.now() - before >= 50.0
    # … while the candle endpoint, a different documented API, still admits.
    started = clock.now()
    gate.admit("upstox.historical_candle")
    assert clock.now() - started < 1.0
    usage = gate.quota_usage("upstox.historical_candle")
    assert usage["1m"] == 1


# --------------------------------------------------------------------------- #
# 429 behaviour: lower the rate, never reset or bypass a window
# --------------------------------------------------------------------------- #
def test_rate_limit_does_not_reset_or_bypass_a_rolling_window() -> None:
    gate, clock = _gate(upstox_standard_policies())
    for _ in range(500):
        gate.admit("upstox.market_quote")
    usage_before = gate.quota_usage("upstox.market_quote")
    assert usage_before["1m"] == 500

    gate._note_rate_limited("upstox.market_quote")  # a provider 429

    # The window still holds the provider's counted requests: the next admission
    # has to wait for it to roll over, and the AIMD cut did not hand it back.
    assert gate.quota_usage("upstox.market_quote")["1m"] == 500
    started = clock.now()
    gate.admit("upstox.market_quote")
    assert clock.now() >= 60.0 and clock.now() - started >= 50.0
    stats = gate.stats()
    assert stats["budget_reductions"] >= 1
    assert stats["effective_requests_per_second"] < stats["config"]["max_requests_per_second"]


def test_guarded_provider_gets_one_retry_for_a_throttle_then_raises() -> None:
    gate, clock = _gate(upstox_standard_policies())
    calls = {"n": 0}

    def provider(symbol: str) -> str:
        calls["n"] += 1
        raise RuntimeError("HTTP 429 Too Many Requests")

    guarded = gate.guarded("upstox.market_quote", provider)
    with pytest.raises(RuntimeError):
        guarded("SYN000")
    assert calls["n"] == 2, "one bounded retry after the shared cooldown"
    stats = gate.stats()
    assert stats["retries"] == 1
    # Two throttles (first attempt + retry), each starting a new cooldown window.
    assert stats["cooldowns"] == 2
    assert stats["quota"]["categories"]["upstox.market_quote"]["rate_limited_events"] == 2


def test_guarded_provider_charges_exactly_one_request_per_call() -> None:
    gate, _ = _gate(upstox_standard_policies())

    def provider(symbol: str) -> str:
        return f"ok-{symbol}"

    guarded = gate.guarded("upstox.historical_candle", provider)
    for i in range(3):
        assert guarded(f"SYN{i:03d}") == f"ok-SYN{i:03d}"
    report = gate.quota_report()["categories"]["upstox.historical_candle"]
    assert report["requests"] == 3
    assert report["windows"]["1m"]["current"] == 3
    assert gate.stats()["requests"] == 3


def test_throttled_symbol_cannot_burn_another_symbols_quota() -> None:
    gate, _ = _gate(upstox_standard_policies())
    attempts = {"n": 0}

    def flaky(symbol: str) -> str:
        if symbol == "BAD":
            attempts["n"] += 1
            raise RuntimeError("HTTP 429 Too Many Requests")
        return symbol

    outcomes = gate.map(["SYN000", "BAD", "SYN001"], flaky, category="upstox.market_quote")
    assert outcomes["BAD"].ok is False
    assert outcomes["SYN000"].ok and outcomes["SYN001"].ok
    # BAD consumed its first attempt + one retry (a retried request is still a
    # request and is charged to the quota); each healthy symbol one.
    assert attempts["n"] == 2
    assert gate.quota_report()["categories"]["upstox.market_quote"]["requests"] == 4


def test_paced_map_charges_one_request_per_symbol_unpaced_does_not() -> None:
    gate, _ = _gate(upstox_standard_policies())
    gate.map(["A", "B"], lambda s: s, category="upstox.market_quote")
    assert gate.quota_report()["categories"]["upstox.market_quote"]["requests"] == 2

    # Unpaced: the callable owns admission (the engine wraps its providers), so
    # map must not charge the quota a second time.
    gate.map(["C", "D"], lambda s: s, paced=False)
    assert gate.quota_report()["categories"]["upstox.market_quote"]["requests"] == 2


# --------------------------------------------------------------------------- #
# The engine draws on the documented categories, and the schedule is bounded
# --------------------------------------------------------------------------- #
def test_engine_scan_charges_the_documented_endpoints() -> None:
    arm = BENCH.Arm(name="quota-scan", symbols=8, latency_ms=1.0, workers=4, rps=200.0,
                    quota_profile="upstox_standard")
    transport = BENCH.FakeMarketData(latency_ms=arm.latency_ms, trend=arm.trend)
    stack = BENCH.build_stack(arm, transport)
    result = stack.engine.analyze_market("NSE", trigger="pytest")
    assert result["counts"]["succeeded"] == 8

    categories = stack.nse_gate.quota_report()["categories"]
    assert categories["upstox.market_quote"]["requests"] == 8
    assert categories["upstox.historical_candle"]["requests"] == 8
    for cat in categories.values():
        for window, payload in cat["windows"].items():
            assert payload["peak"] <= payload["limit"], (window, payload)
    assert transport.calls == 16
    assert result["broker_order_api_calls"] == 0
    stack.engine.close()


def test_schedule_simulation_breaks_15s_and_holds_the_sustainable_cadence() -> None:
    arms = {arm.name: arm for arm in BENCH.schedule_arms(100)}
    fast = BENCH.simulate_schedule(arms["schedule-100syms-every-15s"], minutes=10.0)
    slow = BENCH.simulate_schedule(arms["schedule-100syms-every-90s"], minutes=10.0)

    # 100 symbols every 15 s exceeds the 2,000-per-30-minute window: the gate
    # degrades the cadence instead of breaching the quota.
    assert fast["cycles"]["cadence_held"] is False
    assert fast["cycles"]["cycles_with_data"] < fast["cycles"]["requested"]
    # The gate defers (it never breaches): the deferred cycles produce no data.
    assert fast["cycles"]["cycles_fully_deferred"] > 0
    assert fast["cycles"]["symbols_deferred"] > 0
    for cat in fast["per_endpoint"].values():
        assert cat["within_documented_limits"] is True
        assert cat["peak_in_window"]["30m"] <= 2000
        assert cat["peak_in_window"]["1m"] <= 500
        assert cat["peak_in_window"]["1s"] <= 50

    # The measured sustainable cadence is the documented one (1,800 s x 100 / 2,000).
    assert fast["documented_cadence"]["min_interval_seconds"] == pytest.approx(90.0)
    assert slow["cycles"]["cadence_held"] is True
    assert slow["cycles"]["completed"] == slow["cycles"]["requested"]
    assert slow["cycles"]["cycles_with_data"] == slow["cycles"]["completed"]
    assert slow["cycles"]["symbols_deferred"] == 0
    assert slow["cycles"]["cycle_latency_s"]["p95"] < 90.0


def test_sixteen_symbols_at_fifteen_seconds_stays_inside_the_quota() -> None:
    arm = next(a for a in BENCH.schedule_arms(100) if a.name == "schedule-16syms-every-15s")
    result = BENCH.simulate_schedule(arm, minutes=20.0)
    assert result["cycles"]["cadence_held"] is True
    assert result["cycles"]["completed"] == result["cycles"]["requested"]
    for cat in result["per_endpoint"].values():
        assert cat["within_documented_limits"] is True
        assert cat["peak_in_window"]["30m"] <= 2000


def test_gate_offers_no_way_to_rotate_credentials_or_pools() -> None:
    source = (ROOT / "app" / "markets" / "fetch_gate.py").read_text(encoding="utf-8").lower()
    for forbidden in ("token_pool", "rotate_token", "rotate_credential", "account_pool", "key_ring"):
        assert forbidden not in source
    gate = ProviderFetchGate(FetchGateConfig())
    for attribute in ("rotate", "rotate_credentials", "next_account", "next_credential"):
        assert not hasattr(gate, attribute)
