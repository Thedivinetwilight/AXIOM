"""Regression tests for concurrent multi-market paper analysis, runner isolation,
immediate Start/Resume symbol coverage, exchange calendar discipline, ledger
separation, and strict paper-only safety invariants."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.autonomy.upstox import InstrumentCatalog
from app.config.settings import Settings
from app.data.models import Candle
from app.ensemble.service import StrategyEnsembleService
from app.markets.live_paper import LiveMarketPaperEngine, MultiMarketPaperRunner
from app.markets.nasdaq_catalog import NasdaqCatalog
from app.markets.nse_reconciliation import resolve_nse_instrument_strict

UTC = timezone.utc


def _make_trend_bars(
    symbol: str,
    end_utc: datetime,
    *,
    count: int = 165,
    base_price: float = 200.0,
    drift_pct: float = 0.0055,
    step_minutes: int = 5,
) -> list[Candle]:
    import math

    bars: list[Candle] = []
    px = base_price
    phase = (sum(ord(ch) for ch in symbol) % 11) * 0.45
    start_ts = end_utc - timedelta(minutes=step_minutes * count)
    for i in range(count):
        ts = start_ts + timedelta(minutes=step_minutes * i)
        # Realistic trend + cyclical pullbacks so OOS walk-forward learner observes complete entry/exit cycles
        if i >= count - 6:
            step_ret = drift_pct * 1.4
        else:
            wave = math.sin((i * 0.38) + phase) * 0.009
            step_ret = drift_pct + wave
        nxt = max(1.0, round(px * (1.0 + step_ret), 2))
        hi = round(max(px, nxt) * 1.003, 2)
        lo = round(min(px, nxt) * 0.997, 2)
        vol = 30000.0 if i < count - 6 else 110000.0
        bars.append(
            Candle(
                symbol=symbol,
                timestamp=ts,
                open=px,
                high=hi,
                low=lo,
                close=nxt,
                volume=vol,
            )
        )
        px = nxt
    return bars


def _build_catalogs(tmp_path: Path):
    from app.autonomy.config import AutonomySettings

    nse_cat = InstrumentCatalog(AutonomySettings(), tmp_path / "nse_seed.json")
    nse_cat.load_seed_fallback()
    us_cat = NasdaqCatalog(cache_path=tmp_path / "us_seed.json")
    us_cat.load_seed_fallback()
    return nse_cat, us_cat


class _EventGatedBars:
    """A bars provider that blocks inside the worker until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, sym: str, tf: str) -> list[Candle]:
        self.entered.set()
        # The cycle under test must never be able to unblock this by itself.
        self.release.wait(30)
        return []


def _event_driven_engine(tmp_path: Path, nse_bars_provider, *, label: str = "sync"):
    """Engine/ensemble pair with a fast synthetic NASDAQ and a test-gated NSE."""
    # Wednesday 2026-10-28 14:30 UTC -> NASDAQ OPEN (10:30 EDT), NSE CLOSED (20:00 IST)
    now_utc = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    nse_cat, us_cat = _build_catalogs(tmp_path)
    us_bars = {
        "AAPL": _make_trend_bars("AAPL", now_utc - timedelta(minutes=5), base_price=220.0),
        "MSFT": _make_trend_bars("MSFT", now_utc - timedelta(minutes=5), base_price=410.0),
    }

    def fast_us_quote(sym: str) -> dict[str, Any]:
        px = us_bars[sym][-1].close
        return {
            "symbol": sym,
            "last": px,
            "bid": round(px - 0.02, 2),
            "ask": round(px + 0.02, 2),
            "volume": 120000,
            "timestamp": (now_utc - timedelta(seconds=2)).isoformat(),
            "local_received_at": now_utc.isoformat(),
            "feed": "iex",
            "is_delayed": False,
            "source": "alpaca-iex-test",
        }

    eng = LiveMarketPaperEngine(
        tmp_path / f"{label}_paper.sqlite3",
        nse_bars_provider=nse_bars_provider,
        nasdaq_quote_provider=fast_us_quote,
        nasdaq_bars_provider=lambda sym, tf: us_bars[sym],
        nse_instrument_resolver=lambda s: resolve_nse_instrument_strict(nse_cat, s, allow_index=False),
        nasdaq_instrument_resolver=lambda s: us_cat.get(s).public() if us_cat.get(s) else None,
        clock=lambda: now_utc,
    )
    ens = StrategyEnsembleService(
        tmp_path / f"{label}_ens.sqlite3",
        paper_engine=eng,
        nse_bars_provider=nse_bars_provider,
        nasdaq_quote_provider=fast_us_quote,
        nasdaq_bars_provider=lambda sym, tf: us_bars[sym],
        nse_instrument_resolver=lambda s: resolve_nse_instrument_strict(nse_cat, s, allow_index=False),
        nasdaq_instrument_resolver=lambda s: us_cat.get(s).public() if us_cat.get(s) else None,
        clock=lambda: now_utc,
    )
    eng.ensemble_service = ens
    eng.start_session("NSE", strategy="parallel_ensemble", timeframe="5m", symbols=["RELIANCE"])
    eng.start_session("NASDAQ", strategy="parallel_ensemble", timeframe="5m", symbols=["AAPL", "MSFT"])
    return eng, ens


def _signal_when_market_completes(engine: LiveMarketPaperEngine, market: str,
                                  event: threading.Event) -> None:
    """Fire ``event`` the moment the engine finishes that market's own cycle."""
    original = engine.sync_live_tick

    def wrapper(mkt, **kwargs):
        try:
            return original(mkt, **kwargs)
        finally:
            if mkt == market:
                event.set()

    engine.sync_live_tick = wrapper  # type: ignore[method-assign]


def _run_cycle_in_thread(engine: LiveMarketPaperEngine, *,
                         timeout_seconds: float) -> tuple[threading.Thread, list]:
    out: list = []

    def target():
        out.append(engine.advance_selected_markets_concurrently(
            market_timeout_seconds=timeout_seconds
        ))

    thread = threading.Thread(target=target, name="axiom-cycle-test", daemon=True)
    thread.start()
    return thread, out


def test_slow_nse_provider_never_turns_a_finished_nasdaq_cycle_into_a_timeout(tmp_path: Path) -> None:
    """Deterministic synchronisation: no sleeps, no wall-clock thresholds.

    NSE is parked inside its provider until the test releases it, NASDAQ signals
    its own completion from the worker thread, and only then is the slow provider
    released. Under the old shared-deadline loop the wait spent on NSE was
    deducted from NASDAQ's budget; here the verdicts must be COMPLETED/COMPLETED.
    """
    nse_bars = _EventGatedBars()
    eng, ens = _event_driven_engine(tmp_path, nse_bars, label="never_timeout")
    nasdaq_done = threading.Event()
    _signal_when_market_completes(eng, "NASDAQ", nasdaq_done)
    try:
        thread, out = _run_cycle_in_thread(eng, timeout_seconds=30.0)

        assert nse_bars.entered.wait(10), "the NSE provider was never entered"
        assert nasdaq_done.wait(10), "NASDAQ never finished while NSE was still blocked"
        assert out == [], "the cycle must still be waiting on the slow NSE market"

        nse_bars.release.set()
        thread.join(30)
        assert not thread.is_alive()

        (cycle,) = out
        assert cycle["broker_order_api_calls"] == 0
        assert cycle["live_trading_enabled"] is False
        assert cycle["markets"]["NSE"]["status"] == "COMPLETED"
        assert cycle["markets"]["NASDAQ"]["status"] == "COMPLETED", (
            "a slow NSE provider must never mark a finished NASDAQ cycle as TIMEOUT"
        )
        assert cycle["markets"]["NASDAQ"]["decisions_count"] >= 2
    finally:
        nse_bars.release.set()
        ens.close()
        eng.close()


@pytest.mark.parametrize("market_order", [["NSE", "NASDAQ"], ["NASDAQ", "NSE"]])
def test_market_timeout_budget_is_per_market_and_order_independent(
    tmp_path: Path, market_order: list[str]
) -> None:
    """NSE sits past its own deadline; NASDAQ still finishes inside its own window."""
    nse_bars = _EventGatedBars()
    eng, ens = _event_driven_engine(tmp_path, nse_bars, label="order_" + "_".join(market_order))
    nasdaq_done = threading.Event()
    _signal_when_market_completes(eng, "NASDAQ", nasdaq_done)
    try:
        out: list = []
        thread = threading.Thread(
            target=lambda: out.append(
                eng.advance_selected_markets_concurrently(
                    markets=market_order, market_timeout_seconds=1.50
                )
            ),
            name="axiom-cycle-order-test",
            daemon=True,
        )
        thread.start()
        assert nse_bars.entered.wait(10), "the NSE provider was never entered"
        assert nasdaq_done.wait(10), "NASDAQ never finished inside its own timeout window"
        thread.join(30)
        assert not thread.is_alive()

        (cycle,) = out
        assert cycle["markets"]["NSE"]["status"] == "TIMEOUT"
        assert cycle["markets"]["NASDAQ"]["status"] == "COMPLETED"
        assert cycle["markets"]["NSE"]["broker_order_api_calls"] == 0
        assert cycle["markets"]["NASDAQ"]["broker_order_api_calls"] == 0
    finally:
        nse_bars.release.set()
        ens.close()
        eng.close()


def test_background_runner_isolates_a_blocked_market_without_sleeps(tmp_path: Path) -> None:
    """The async runner keeps independent per-market timeouts (event-driven check)."""
    nse_bars = _EventGatedBars()
    eng, ens = _event_driven_engine(tmp_path, nse_bars, label="runner")
    nasdaq_done = threading.Event()
    _signal_when_market_completes(eng, "NASDAQ", nasdaq_done)

    async def _run() -> dict[str, Any]:
        runner = MultiMarketPaperRunner(eng, interval_seconds=5.0, market_timeout_seconds=3.0)
        try:
            return await runner.run_once(only_active=True)
        finally:
            await runner.stop()

    try:
        # NSE stays blocked for the whole run; NASDAQ must still complete on its
        # own budget. Completion is proven by the worker-set event, not by elapsed
        # wall time.
        runner_out = asyncio.run(_run())
        assert nse_bars.entered.is_set(), "the NSE provider was never entered"
        assert nasdaq_done.is_set(), "NASDAQ never finished while NSE was blocked"
        assert runner_out["markets"]["NSE"]["status"] == "TIMEOUT"
        assert runner_out["markets"]["NASDAQ"]["status"] == "COMPLETED"
        assert runner_out["broker_order_api_calls"] == 0
    finally:
        nse_bars.release.set()
        ens.close()
        eng.close()


def test_exchange_calendar_open_vs_closed_and_full_ensemble_idempotency(tmp_path: Path) -> None:
    """Closed market analyzes historical bars with 0 trades while open market paper-trades all enabled strategies."""
    current_clock = [datetime(2026, 10, 28, 14, 30, tzinfo=UTC)]  # 20:00 IST (NSE CLOSED) / 10:30 EDT (NASDAQ OPEN)
    nse_cat, us_cat = _build_catalogs(tmp_path)

    def nse_bars_fn(sym: str, tf: str) -> list[Candle]:
        key = sym.split("|")[-1].upper()
        base = 1400.0 if key == "RELIANCE" else 3600.0
        return _make_trend_bars(key, current_clock[0] - timedelta(minutes=5), base_price=base)

    def us_bars_fn(sym: str, tf: str) -> list[Candle]:
        base = 230.0 if sym.upper() == "AAPL" else 420.0
        return _make_trend_bars(sym.upper(), current_clock[0] - timedelta(minutes=5), base_price=base)

    def nse_quote_fn(ikey: str) -> dict[str, Any]:
        sym_name = "RELIANCE" if "INE002A01018" in ikey else "TCS"
        px = nse_bars_fn(sym_name, "5m")[-1].close
        return {
            "instrument_key": ikey,
            "last": px,
            "bid": round(px - 0.10, 2),
            "ask": round(px + 0.10, 2),
            "volume": 90000,
            "timestamp": (current_clock[0] - timedelta(seconds=2)).isoformat(),
            "local_received_at": current_clock[0].isoformat(),
            "is_delayed": False,
            "source": "upstox-v2-test",
        }

    def us_quote_fn(sym: str) -> dict[str, Any]:
        bars = us_bars_fn(sym, "5m")
        px = bars[-1].close
        return {
            "symbol": sym,
            "last": px,
            "bid": round(px - 0.02, 2),
            "ask": round(px + 0.02, 2),
            "volume": 150000,
            "timestamp": (current_clock[0] - timedelta(seconds=2)).isoformat(),
            "local_received_at": current_clock[0].isoformat(),
            "feed": "iex",
            "is_delayed": False,
            "source": "alpaca-iex-test",
        }

    eng = LiveMarketPaperEngine(
        tmp_path / "cal_paper.sqlite3",
        nse_quote_provider=nse_quote_fn,
        nasdaq_quote_provider=us_quote_fn,
        nse_bars_provider=nse_bars_fn,
        nasdaq_bars_provider=us_bars_fn,
        nse_instrument_resolver=lambda s: resolve_nse_instrument_strict(nse_cat, s, allow_index=False),
        nasdaq_instrument_resolver=lambda s: us_cat.get(s).public() if us_cat.get(s) else None,
        clock=lambda: current_clock[0],
    )
    ens = StrategyEnsembleService(
        tmp_path / "cal_ens.sqlite3",
        paper_engine=eng,
        nse_quote_provider=nse_quote_fn,
        nasdaq_quote_provider=us_quote_fn,
        nse_bars_provider=nse_bars_fn,
        nasdaq_bars_provider=us_bars_fn,
        nse_instrument_resolver=lambda s: resolve_nse_instrument_strict(nse_cat, s, allow_index=False),
        nasdaq_instrument_resolver=lambda s: us_cat.get(s).public() if us_cat.get(s) else None,
        clock=lambda: current_clock[0],
    )
    eng.ensemble_service = ens

    try:
        enabled_variants = ens.registry.list_variants(enabled_only=True)
        assert len(enabled_variants) >= 10

        eng.start_session("NSE", strategy="parallel_ensemble", timeframe="5m", symbols=["RELIANCE", "TCS"])
        eng.start_session("NASDAQ", strategy="parallel_ensemble", timeframe="5m", symbols=["AAPL", "MSFT"])

        # Phase 1: NASDAQ is OPEN, NSE is CLOSED
        c1 = eng.advance_selected_markets_concurrently()
        nse_s1 = c1["markets"]["NSE"]["snapshot"]
        us_s1 = c1["markets"]["NASDAQ"]["snapshot"]

        # Closed NSE was analyzed across all enabled variants, placed ZERO simulated orders, and kept INR intact
        assert nse_s1["session_state"] == "MARKET_CLOSED_WAITING"
        assert nse_s1["currency"] == "INR"
        assert len(nse_s1["orders"]) == 0
        assert len(nse_s1["positions"]) == 0
        assert nse_s1["last_analysis"]["counts"]["succeeded"] == 2
        assert nse_s1["last_analysis"]["trade_execution_permitted_now"] is False
        assert nse_s1["last_analysis"]["enabled_strategy_variants"] == len(enabled_variants)

        # Open NASDAQ evaluated all enabled variants together and placed simulated USD order(s)
        assert us_s1["session_state"] == "RUNNING"
        assert us_s1["currency"] == "USD"
        assert len(us_s1["decisions"]) == 2
        us_orders_phase1 = len(us_s1["orders"])
        assert us_orders_phase1 >= 1
        assert all(o["market"] == "NASDAQ" and o["currency"] == "USD" for o in us_s1["orders"])

        # Re-running on the SAME completed bar timestamp skips duplicate orders (bar idempotency)
        c1_dup = eng.advance_selected_markets_concurrently()
        assert len(c1_dup["markets"]["NASDAQ"]["snapshot"]["orders"]) == us_orders_phase1

        # Phase 2: Advance clock to Thursday 2026-10-29 06:00 UTC -> 11:30 IST (NSE OPEN), 02:00 EDT (NASDAQ CLOSED)
        current_clock[0] = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
        c2 = eng.advance_selected_markets_concurrently()
        nse_s2 = c2["markets"]["NSE"]["snapshot"]
        us_s2 = c2["markets"]["NASDAQ"]["snapshot"]

        # NSE reconnected to RUNNING on open IST session and executed INR paper decisions independently
        assert nse_s2["session_state"] == "RUNNING"
        assert len(nse_s2["decisions"]) == 2
        assert len(nse_s2["orders"]) >= 1
        assert all(o["market"] == "NSE" and o["currency"] == "INR" for o in nse_s2["orders"])

        # NASDAQ transitioned to MARKET_CLOSED_WAITING, analyzed bars read-only, and placed ZERO new USD orders
        assert us_s2["session_state"] == "MARKET_CLOSED_WAITING"
        assert len(us_s2["orders"]) == us_orders_phase1
        assert us_s2["last_analysis"]["trade_execution_permitted_now"] is False
    finally:
        ens.close()
        eng.close()


def test_immediate_start_and_resume_symbol_coverage_selection_preservation_and_honest_chart(
    tmp_path: Path,
) -> None:
    """API Start & Resume immediately analyze every selected symbol, report successes/missing/failures,
    preserve ledgers across selection changes, redact chart errors, and keep LIVE disarmed."""
    settings = replace(Settings(), api_token="mm-cal-tok", database_url=f"sqlite:///{tmp_path}/mm_api.db")
    headers = {"Authorization": "Bearer mm-cal-tok"}

    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        eng: LiveMarketPaperEngine = web.app.state.live_paper_engine
        ens: StrategyEnsembleService = web.app.state.ensemble_service
        now_open_us = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
        eng._clock = lambda: now_open_us
        ens._now = lambda: now_open_us

        good_aapl = _make_trend_bars("AAPL", now_open_us - timedelta(minutes=5), base_price=225.0)

        def mixed_us_bars(sym: str, tf: str) -> list[Candle]:
            s = sym.upper()
            if s == "AAPL":
                return good_aapl
            if s == "MSFT":
                return []  # Missing data
            raise RuntimeError("Upstream 503 for NVDA with Bearer super-secret-key-999")

        def mixed_us_quote(sym: str) -> dict[str, Any]:
            px = good_aapl[-1].close
            return {
                "symbol": sym.upper(),
                "last": px,
                "bid": round(px - 0.02, 2),
                "ask": round(px + 0.02, 2),
                "volume": 80000,
                "timestamp": (now_open_us - timedelta(seconds=2)).isoformat(),
                "local_received_at": now_open_us.isoformat(),
                "feed": "iex",
                "is_delayed": False,
                "source": "alpaca-iex-test",
            }

        eng.nasdaq_bars_provider = mixed_us_bars
        eng.nasdaq_quote_provider = mixed_us_quote
        ens.nasdaq_bars_provider = mixed_us_bars
        ens.nasdaq_quote_provider = mixed_us_quote

        # Configure watchlist to exactly AAPL (ok), MSFT (missing), NVDA (failure)
        for existing in list(eng.list_watchlists()["NASDAQ"]):
            eng.remove_watchlist_symbol("NASDAQ", existing["symbol"])
        for sym in ("AAPL", "MSFT", "NVDA"):
            eng.add_watchlist_symbol("NASDAQ", sym)

        # Multi-market Start immediately analyzes all selected markets & symbols
        start_res = web.post(
            "/api/markets/paper/sessions/start",
            headers=headers,
            json={
                "markets": ["NSE", "NASDAQ"],
                "strategy_by_market": {"NSE": "parallel_ensemble", "NASDAQ": "parallel_ensemble"},
                "timeframe_by_market": {"NSE": "5m", "NASDAQ": "5m"},
                "execute_immediately": False,
            },
        )
        assert start_res.status_code == 200
        start_payload = start_res.json()
        assert start_payload["live_trading_enabled"] is False
        assert start_payload["broker_order_api_calls"] == 0

        us_cov = start_payload["markets"]["NASDAQ"]["initial_analysis"]
        assert us_cov["counts"]["selected_symbols"] == 3
        assert us_cov["succeeded_symbols"] == ["AAPL"]
        assert us_cov["missing_data_symbols"] == ["MSFT"]
        assert us_cov["failed_symbols"] == ["NVDA"]

        # Pause & Resume immediately re-analyzes every selected symbol and preserves state
        pause_res = web.post(
            "/api/markets/paper/sessions/transition",
            headers=headers,
            json={"action": "pause", "markets": ["NASDAQ"]},
        )
        assert pause_res.status_code == 200
        assert pause_res.json()["markets"]["NASDAQ"]["session_state"] == "PAUSED"

        resume_res = web.post(
            "/api/markets/paper/sessions/transition",
            headers=headers,
            json={"action": "resume", "markets": ["NASDAQ"], "execute_immediately": False},
        )
        assert resume_res.status_code == 200
        res_cov = resume_res.json()["markets"]["NASDAQ"]["initial_analysis"]
        assert res_cov["trigger"] == "session_resume"
        assert res_cov["succeeded_symbols"] == ["AAPL"]
        assert res_cov["missing_data_symbols"] == ["MSFT"]
        assert res_cov["failed_symbols"] == ["NVDA"]

        # Changing market selection never resets cash, positions, orders, or watchlists
        sub_ord = eng.submit_order(market="NASDAQ", symbol="AAPL", side="BUY", qty=2, order_type="MARKET")
        assert sub_ord["ok"] is True
        cash_before = eng.market_snapshot("NASDAQ")["account"]["cash"]

        sel_res = web.post("/api/markets/paper/selection", headers=headers, json={"markets": ["NSE"]})
        assert sel_res.status_code == 200
        assert sel_res.json()["selected_markets"] == ["NSE"]
        us_after_deselect = eng.market_snapshot("NASDAQ")
        assert us_after_deselect["account"]["cash"] == cash_before
        assert len(us_after_deselect["positions"]) == 1

        # Structured logs include PAPER_MARKET_ANALYSIS_REPORT and PAPER_SYMBOL_SKIPPED with secret redaction
        logs_res = web.get("/api/markets/paper/logs?market=NASDAQ&limit=50", headers=headers).json()
        ev_types = {e["event_type"] for e in logs_res["events"]}
        assert "PAPER_MARKET_ANALYSIS_REPORT" in ev_types
        assert "PAPER_SYMBOL_SKIPPED" in ev_types
        serialized_logs = str(logs_res)
        assert "super-secret-key-999" not in serialized_logs

        # Overview confirms LIVE execution remains OFF and runner is paper-only
        ov = web.get("/api/markets/overview", headers=headers).json()
        assert ov["live"]["default_live_execution"] == "OFF"
        assert ov["runner"]["live_trading_enabled"] is False
        assert ov["runner"]["broker_order_api_calls"] == 0


def test_chart_endpoint_surfaces_honest_redacted_provider_error_without_sample_fallback(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """When Upstox throws a generic network/HTTP error, /api/markets/chart returns empty bars,
    simulated_bars=False, and a secret-redacted error message."""
    from app.autonomy.upstox import UpstoxReadOnly
    import app.api.routes.markets as mkt_routes

    def boom_candles(self: Any, *args: Any, **kwargs: Any) -> list[Candle]:
        raise ConnectionError("Upstox gateway timeout Authorization: Bearer secret-upstox-jwt-777")

    monkeypatch.setattr(UpstoxReadOnly, "intraday_candles", boom_candles)
    monkeypatch.setattr(UpstoxReadOnly, "historical_candles", boom_candles)
    monkeypatch.setattr(
        mkt_routes,
        "verify_upstox_market_data",
        lambda *a, **k: {"connection_state": "UNAVAILABLE", "authenticated": False, "quote": None},
    )

    settings = replace(
        Settings(),
        api_token="chart-tok",
        market_data_access_token="secret-upstox-jwt-777",
        database_url=f"sqlite:///{tmp_path}/chart_err.db",
    )
    headers = {"Authorization": "Bearer chart-tok"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        res = web.get("/api/markets/chart?market=NSE&symbol=RELIANCE&timeframe=5m", headers=headers)
        assert res.status_code == 200
        payload = res.json()
        assert payload["ok"] is False
        assert payload["bars"] == []
        assert payload["simulated_bars"] is False
        assert payload["real_order_api_used"] is False
        assert payload["connection_state"] == "UNAVAILABLE"
        assert payload["error"] and "Upstox gateway timeout" in payload["error"]
        assert "secret-upstox-jwt-777" not in str(payload)

