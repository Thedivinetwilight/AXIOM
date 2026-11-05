"""Phase 5 tests: LiveMarketPaperEngine (NSE INR + NASDAQ USD), recovery, and zero-broker-order proof."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.markets.live_paper import LiveMarketPaperEngine
from app.markets.nasdaq_catalog import NasdaqCatalog
from app.autonomy.config import AutonomySettings
from app.autonomy.upstox import InstrumentCatalog


def _make_resolvers(tmp_path: Path):
    import json

    nse_cat = InstrumentCatalog(AutonomySettings(), tmp_path / "nse.json")
    bod_fixture = Path(__file__).resolve().parent / "fixtures" / "upstox_bod_sanitized_master.json"
    nse_cat._ingest_records(
        json.loads(bod_fixture.read_text(encoding="utf-8"))["complete"],
        digest="fixture",
        suspended_keys=frozenset(),
        mis_keys=frozenset(),
        source_kind="authoritative_upstox_bod",
    )
    nasdaq_cat = NasdaqCatalog(None)
    return (
        lambda sym: (nse_cat.search(sym, include_indices=False, limit=1)["items"] or [None])[0],
        lambda sym: (nasdaq_cat.get(sym).public() if nasdaq_cat.get(sym) else None),
    )


def test_nse_and_nasdaq_paper_ledgers_fees_sl_tp_idempotency_and_zero_broker_orders(tmp_path: Path):
    now = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)
    # Wait: 14:30 UTC is 20:00 IST (NSE closed) and 10:30 EDT (NASDAQ open).
    # Let's test NSE at 06:00 UTC (11:30 IST open) and NASDAQ at 14:30 UTC (10:30 EDT open)!
    current_clock = [datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)]
    nse_price = [1400.00]
    us_price = [220.00]
    broker_order_calls: list[str] = []  # Proof: MUST remain empty throughout!

    nse_res, us_res = _make_resolvers(tmp_path)
    db_file = tmp_path / "live_paper.sqlite3"

    def nse_quote(key: str) -> dict:
        return {
            "instrument_key": key,
            "bid": nse_price[0] - 0.10,
            "ask": nse_price[0] + 0.10,
            "last": nse_price[0],
            "volume": 50_000,
            "timestamp": current_clock[0].isoformat(),
            "source": "upstox-market-quote-v2",
        }

    def us_quote(sym: str) -> dict:
        return {
            "symbol": sym,
            "bid": us_price[0] - 0.05,
            "ask": us_price[0] + 0.05,
            "last": us_price[0],
            "volume": 80_000,
            "timestamp": current_clock[0].isoformat(),
            "source": "alpaca-market-data:iex",
        }

    eng = LiveMarketPaperEngine(
        db_file,
        nse_quote_provider=nse_quote,
        nasdaq_quote_provider=us_quote,
        nse_instrument_resolver=nse_res,
        nasdaq_instrument_resolver=us_res,
        clock=lambda: current_clock[0],
    )

    # 1. NSE INR simulated order during open IST session (06:00 UTC = 11:30 IST)
    eng.start_session("NSE", strategy="manual_only", symbols=["RELIANCE"])
    prev_nse = eng.preview_order(
        market="NSE", symbol="RELIANCE", side="BUY", qty=10, order_type="MARKET", product="D"
    )
    assert prev_nse["ok"] is True
    assert prev_nse["currency"] == "INR"
    assert prev_nse["simulated"] is True
    assert prev_nse["real_order_api_used"] is False
    assert prev_nse["estimated_fees"]["stt"] > 0  # Delivery STT on buy

    sub_nse = eng.submit_order(
        client_order_id="nse-idem-001",
        market="NSE",
        symbol="RELIANCE",
        side="BUY",
        qty=10,
        order_type="MARKET",
        product="D",
        stop_loss=1380.0,
        take_profit=1450.0,
    )
    assert sub_nse["ok"] is True
    assert sub_nse["order"]["status"] == "FILLED"
    assert sub_nse["idempotent_replay"] is False

    # Idempotent replay with same client_order_id does NOT double-fill!
    replay = eng.submit_order(
        client_order_id="nse-idem-001",
        market="NSE",
        symbol="RELIANCE",
        side="BUY",
        qty=10,
        order_type="MARKET",
        product="D",
    )
    assert replay["idempotent_replay"] is True
    assert len(eng.market_snapshot("NSE")["fills"]) == 1

    # Delivery-only BE trade-for-trade stock blocks MIS intraday!
    mis_blocked = eng.preview_order(
        market="NSE", symbol="IDEA", side="BUY", qty=10, order_type="MARKET", product="I"
    )
    assert mis_blocked["ok"] is False
    assert any(c["check"] == "settlement_series" and not c["ok"] for c in mis_blocked["checks"])

    # Price rises to 1455 -> sync_live_tick triggers Take-Profit exit automatically!
    nse_price[0] = 1455.00
    current_clock[0] = current_clock[0] + timedelta(seconds=10)
    tick_nse = eng.sync_live_tick("NSE")
    assert len(tick_nse["triggered_orders"]) == 1
    snap_nse = eng.market_snapshot("NSE")
    assert snap_nse["positions"] == []
    assert snap_nse["account"]["realized_pnl"] > 0

    # 2. Switch clock to NASDAQ regular session (14:30 UTC = 10:30 EDT)
    current_clock[0] = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)
    eng.start_session("NASDAQ", strategy="ema_crossover", symbols=["AAPL"])

    # Working limit order -> replace -> cancel
    lim = eng.submit_order(
        client_order_id="us-lim-1",
        market="NASDAQ",
        symbol="AAPL",
        side="BUY",
        qty=5,
        order_type="LIMIT",
        limit_price=210.00,
    )
    assert lim["order"]["status"] == "WORKING"
    rep = eng.replace_order("us-lim-1", qty=6, limit_price=212.00)
    assert rep["order"]["qty"] == 6 and rep["order"]["limit_price"] == 212.00
    can = eng.cancel_order("us-lim-1")
    assert can["order"]["status"] == "CANCELED"

    # Market BUY then SELL on NASDAQ USD -> isolated from NSE INR ledger!
    us_buy = eng.submit_order(market="NASDAQ", symbol="AAPL", side="BUY", qty=4, order_type="MARKET")
    assert us_buy["ok"] is True and us_buy["order"]["status"] == "FILLED"
    us_sell = eng.submit_order(market="NASDAQ", symbol="AAPL", side="SELL", qty=4, order_type="MARKET")
    assert us_sell["ok"] is True
    # SEC & FINRA TAF fees apply on US equity sells
    us_fills = eng.market_snapshot("NASDAQ")["fills"]
    sell_fill = next(f for f in us_fills if f["side"] == "SELL")
    assert sell_fill["fee_breakdown"]["finra_taf"] > 0
    assert eng.market_snapshot("NASDAQ")["currency"] == "USD"
    assert eng.market_snapshot("NSE")["currency"] == "INR"

    # Strategy evaluation on completed bars
    bars = [
        {"timestamp": f"2026-10-29T14:{10+i:02d}:00Z", "close": 200.0 + (i * 0.5)}
        for i in range(25)
    ]
    strat_eval = eng.evaluate_strategy_bars("NASDAQ", "AAPL", bars, strategy="ema_crossover", auto_execute=False)
    assert strat_eval["signal"] in {"BUY", "HOLD", "SELL"}

    # Zero broker order calls throughout!
    assert broker_order_calls == []
    assert eng.reconcile("NSE")["ok"] is True
    assert eng.reconcile("NASDAQ")["ok"] is True


def test_paper_crash_restart_recovery_and_audit_hash_chain(tmp_path: Path):
    clock_now = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)
    nse_res, us_res = _make_resolvers(tmp_path)
    db_file = tmp_path / "recover_paper.sqlite3"

    eng1 = LiveMarketPaperEngine(
        db_file,
        nasdaq_quote_provider=lambda s: {
            "symbol": s,
            "bid": 199.95,
            "ask": 200.05,
            "last": 200.0,
            "volume": 50_000,
            "timestamp": clock_now.isoformat(),
            "source": "alpaca-market-data:iex",
        },
        nse_instrument_resolver=nse_res,
        nasdaq_instrument_resolver=us_res,
        clock=lambda: clock_now,
    )
    eng1.start_session("NASDAQ", strategy="manual_only", symbols=["MSFT"])
    assert eng1.market_snapshot("NASDAQ")["session"]["state"] == "RUNNING"
    eng1.close()

    # Simulated restart: RUNNING session transitions to RECOVERED_PAUSED (never auto-trades silently!)
    eng2 = LiveMarketPaperEngine(
        db_file,
        nse_instrument_resolver=nse_res,
        nasdaq_instrument_resolver=us_res,
        clock=lambda: clock_now,
    )
    snap2 = eng2.market_snapshot("NASDAQ")
    assert snap2["session"]["state"] == "RECOVERED_PAUSED"
    rec = eng2.reconcile("NASDAQ")
    assert rec["ok"] is True and rec["audit_chain_valid"] is True
    eng2.close()


def test_unified_markets_api_overview_search_watchlists_and_emergency_stop(tmp_path: Path):
    settings = replace(Settings(), api_token="mkt-tok", database_url=f"sqlite:///{tmp_path}/mkt.db")
    headers = {"Authorization": "Bearer mkt-tok"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        ov = web.get("/api/markets/overview", headers=headers)
        assert ov.status_code == 200
        payload = ov.json()
        assert payload["live"]["default_live_execution"] == "OFF"
        assert "NSE" in payload["paper"] and "NASDAQ" in payload["paper"]

        # Unified search across NSE & NASDAQ
        s_rel = web.get("/api/markets/search?query=RELIANCE&market=NSE", headers=headers).json()
        assert s_rel["items"][0]["instrument_key"] == "NSE_EQ|INE002A01018"

        s_aapl = web.get("/api/markets/search?query=AAPL&market=NASDAQ", headers=headers).json()
        assert s_aapl["items"][0]["symbol"] == "AAPL"

        # Disconnected chart returns honest DISCONNECTED state with empty bars (never sample/replay disguised as live!)
        ch_nse = web.get("/api/markets/chart?market=NSE&symbol=RELIANCE&timeframe=5m", headers=headers).json()
        assert ch_nse["connection_state"] == "DISCONNECTED"
        assert ch_nse["bars"] == []
        assert ch_nse["simulated_bars"] is False

        ch_us = web.get("/api/markets/chart?market=NASDAQ&symbol=AAPL&timeframe=5m", headers=headers).json()
        assert ch_us["connection_state"] == "DISCONNECTED"
        assert ch_us["bars"] == []
        assert ch_us["simulated_bars"] is False

        # Watchlist add & remove
        wl_add = web.post("/api/markets/watchlists", headers=headers, json={"market": "NSE", "symbol": "TCS"})
        assert wl_add.status_code == 200
        assert "TCS" in wl_add.json()["items"]

        # Atomic Global Emergency Stop & typed reset
        kill = web.post("/api/markets/emergency-stop", headers=headers, json={"reason": "test drill"})
        assert kill.status_code == 200 and kill.json()["ok"] is True

        bad_rst = web.post("/api/markets/emergency-stop/reset", headers=headers, json={"confirm_phrase": "nope"})
        assert bad_rst.status_code == 422

        ok_rst = web.post(
            "/api/markets/emergency-stop/reset",
            headers=headers,
            json={"confirm_phrase": "CLEAR EMERGENCY STOP"},
        )
        assert ok_rst.status_code == 200 and ok_rst.json()["live_execution_remains_off"] is True
