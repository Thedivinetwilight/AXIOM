"""Tests for the strictly Read-Only 'Live Trade Monitor' workspace (v3.7.0a8):
- GET-only read-only route audit & zero order-mutation side effects
- 8 visually distinct event marker types/shapes & full inspection fields
- Unmistakable PAPER (simulated) vs LIVE (broker-confirmed) separation
- Honest 'NOT TRADING' banner when stopped, paused, disarmed, or scanning
- Fast pagination across 550+ monitored instruments without freezing
- Stale/missing/disconnected market-data handling with zero sample substitution
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.markets.live_paper import LiveMarketPaperEngine
from app.markets.trade_monitor import (
    MARKER_SHAPES,
    build_instrument_event_markers,
    build_trade_monitor_summary,
    compute_session_trading_banner,
)


class _StubPaperSnapshotEngine:
    def __init__(self, snapshots: dict[str, dict[str, Any]]) -> None:
        self._snapshots = snapshots

    def market_snapshot(self, market: str) -> dict[str, Any]:
        return self._snapshots[market.upper()]


class _StubEnsembleService:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self._decisions = decisions

    def list_recent_decisions(
        self, *, market: str | None = None, symbol: str | None = None, limit: int = 40
    ) -> list[dict[str, Any]]:
        out = self._decisions
        if market:
            out = [d for d in out if d["market"] == market.upper()]
        if symbol:
            out = [d for d in out if d["symbol"] == symbol.upper()]
        return out[:limit]


class _StubAlpacaJournal:
    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self._orders = orders

    def list_orders(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._orders[:limit]


class _StubAlpacaService:
    def __init__(self, gate_state: str, orders: list[dict[str, Any]]) -> None:
        self._gate_state = gate_state
        self.journal = _StubAlpacaJournal(orders)

    def status(self) -> dict[str, Any]:
        return {"gate": {"state": self._gate_state}, "positions": []}


class TestSessionTradingBannerHonesty:
    def test_not_trading_when_stopped_paused_disarmed_or_merely_scanning(self) -> None:
        # 1. Stopped
        b_stopped = compute_session_trading_banner(
            execution_mode="LIVE_MARKET_PAPER",
            session_state="STOPPED",
            strategy="parallel_ensemble",
            open_positions_count=0,
            working_orders_count=0,
        )
        assert b_stopped["actively_trading"] is False
        assert "NOT TRADING" in b_stopped["banner_status"]

        # 2. Paused even with an open position
        b_paused = compute_session_trading_banner(
            execution_mode="LIVE_MARKET_PAPER",
            session_state="PAUSED",
            strategy="parallel_ensemble",
            open_positions_count=1,
            working_orders_count=0,
        )
        assert b_paused["actively_trading"] is False
        assert "NOT TRADING" in b_paused["banner_status"]

        # 3. Disarmed LIVE mode
        b_live_disarmed = compute_session_trading_banner(
            execution_mode="LIVE_MARKET_LIVE",
            session_state="DISABLED",
            strategy="operator_reviewed",
            open_positions_count=0,
            working_orders_count=0,
        )
        assert b_live_disarmed["actively_trading"] is False
        assert "NOT TRADING" in b_live_disarmed["banner_status"]

        # 4. RUNNING session that is ONLY scanning (zero positions, zero working orders) -> NOT TRADING!
        b_scanning = compute_session_trading_banner(
            execution_mode="LIVE_MARKET_PAPER",
            session_state="RUNNING",
            strategy="parallel_ensemble",
            open_positions_count=0,
            working_orders_count=0,
        )
        assert b_scanning["actively_trading"] is False
        assert "NOT TRADING" in b_scanning["banner_status"]

        # 5. RUNNING paper session WITH confirmed open position -> TRADING ACTIVE · PAPER (SIMULATED)
        b_active_paper = compute_session_trading_banner(
            execution_mode="LIVE_MARKET_PAPER",
            session_state="RUNNING",
            strategy="parallel_ensemble",
            open_positions_count=1,
            working_orders_count=0,
        )
        assert b_active_paper["actively_trading"] is True
        assert "SIMULATED" in b_active_paper["banner_status"]


class TestEightDistinctMarkersAndPaperVsLiveSeparation:
    def test_all_eight_marker_shapes_are_unique_and_carry_full_inspection_metadata(self) -> None:
        expected_types = {
            "SIGNAL",
            "ORDER_SUBMITTED",
            "PARTIAL_FILL",
            "COMPLETED_FILL",
            "CANCELED",
            "STOP_LOSS",
            "TAKE_PROFIT",
            "EXIT",
        }
        assert set(MARKER_SHAPES.keys()) == expected_types
        # Every marker type MUST have a unique geometric shape (signals never look like orders/fills)
        assert len(set(MARKER_SHAPES.values())) == 8
        assert MARKER_SHAPES["SIGNAL"] != MARKER_SHAPES["ORDER_SUBMITTED"]
        assert MARKER_SHAPES["SIGNAL"] != MARKER_SHAPES["COMPLETED_FILL"]

        t0 = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
        snap = {
            "market": "NASDAQ",
            "currency": "USD",
            "session_state": "RUNNING",
            "strategy": "parallel_ensemble",
            "watchlist": [{"symbol": "AAPL"}],
            "positions": [
                {
                    "symbol": "AAPL",
                    "qty": 18.0,
                    "avg_price": 225.75,
                    "last_price": 227.00,
                    "stop_loss": 221.00,
                    "take_profit": 233.00,
                    "updated_at": (t0 + timedelta(minutes=6)).isoformat(),
                }
            ],
            "orders": [
                {
                    "client_order_id": "ord_entry_1",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "qty": 10.0,
                    "filled_qty": 10.0,
                    "avg_fill_price": 225.50,
                    "fees_total": 0.05,
                    "status": "FILLED",
                    "created_at": (t0 + timedelta(minutes=5)).isoformat(),
                    "updated_at": (t0 + timedelta(minutes=5, seconds=2)).isoformat(),
                },
                {
                    "client_order_id": "ord_partial_2",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "qty": 20.0,
                    "filled_qty": 8.0,
                    "limit_price": 226.10,
                    "avg_fill_price": 226.10,
                    "fees_total": 0.04,
                    "status": "PARTIALLY_FILLED",
                    "created_at": (t0 + timedelta(minutes=10)).isoformat(),
                    "updated_at": (t0 + timedelta(minutes=10, seconds=3)).isoformat(),
                },
                {
                    "client_order_id": "ord_cancel_3",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "qty": 5.0,
                    "filled_qty": 0.0,
                    "limit_price": 220.00,
                    "fees_total": 0.0,
                    "status": "CANCELED",
                    "reject_reason": "Stale limit canceled",
                    "created_at": (t0 + timedelta(minutes=15)).isoformat(),
                    "updated_at": (t0 + timedelta(minutes=16)).isoformat(),
                },
                {
                    "client_order_id": "lmp-close-exit-4",
                    "symbol": "AAPL",
                    "side": "SELL",
                    "order_type": "MARKET",
                    "qty": 5.0,
                    "filled_qty": 5.0,
                    "avg_fill_price": 233.10,
                    "fees_total": 0.08,
                    "status": "FILLED",
                    "created_at": (t0 + timedelta(minutes=25)).isoformat(),
                    "updated_at": (t0 + timedelta(minutes=25, seconds=1)).isoformat(),
                },
            ],
            "fills": [
                {
                    "fill_id": "f1",
                    "client_order_id": "ord_entry_1",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "fill_qty": 10.0,
                    "fill_price": 225.50,
                    "fees_total": 0.05,
                    "quote_source": "alpaca_iex_readonly",
                    "created_at": (t0 + timedelta(minutes=5, seconds=2)).isoformat(),
                },
                {
                    "fill_id": "f2",
                    "client_order_id": "ord_partial_2",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "fill_qty": 8.0,
                    "fill_price": 226.10,
                    "fees_total": 0.04,
                    "quote_source": "alpaca_iex_readonly",
                    "created_at": (t0 + timedelta(minutes=10, seconds=3)).isoformat(),
                },
                {
                    "fill_id": "f3",
                    "client_order_id": "lmp-close-exit-4",
                    "symbol": "AAPL",
                    "side": "SELL",
                    "fill_qty": 5.0,
                    "fill_price": 233.10,
                    "fees_total": 0.08,
                    "quote_source": "alpaca_iex_readonly",
                    "created_at": (t0 + timedelta(minutes=25, seconds=1)).isoformat(),
                },
            ],
            "decisions": [],
        }
        ens_svc = _StubEnsembleService(
            [
                {
                    "decision_id": "dec_aapl_1",
                    "market": "NASDAQ",
                    "symbol": "AAPL",
                    "bar_timestamp": (t0 + timedelta(minutes=4)).isoformat(),
                    "final_action": "BUY",
                    "selected_qty": 10,
                    "reference_price": 225.45,
                    "estimated_fees": 0.05,
                    "calibration": {"calibrated_confidence": 0.61},
                    "no_trade_reasons": [],
                }
            ]
        )
        stub_engine = _StubPaperSnapshotEngine({"NASDAQ": snap, "NSE": {**snap, "market": "NSE", "positions": [], "orders": [], "fills": [], "watchlist": []}})

        res = build_instrument_event_markers(
            market="NASDAQ",
            symbol="AAPL",
            execution_mode="LIVE_MARKET_PAPER",
            paper_engine=stub_engine,
            ensemble_service=ens_svc,
        )
        markers = res["markers"]
        present_types = {m["marker_type"] for m in markers}
        assert present_types == expected_types

        required_fields = {
            "marker_id",
            "marker_type",
            "shape",
            "event_time",
            "side",
            "quantity",
            "price",
            "fees",
            "strategy",
            "reason",
            "order_or_fill_status",
            "execution_mode",
            "simulated",
            "verification_origin",
        }
        for m in markers:
            assert required_fields.issubset(m.keys())
            assert m["simulated"] is True
            assert m["execution_mode"] == "LIVE_MARKET_PAPER"
            assert "BROKER_CONFIRMED" not in m["verification_origin"]

        # Verify LIVE broker-confirmed journal entries are unmistakably marked simulated=False
        live_alpaca = _StubAlpacaService(
            "DISABLED",
            [
                {
                    "client_order_id": "live_ord_99",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "qty": 2,
                    "filled_qty": 2,
                    "filled_avg_price": 227.00,
                    "state": "FILLED",
                    "created_at": (t0 + timedelta(minutes=30)).isoformat(),
                    "updated_at": (t0 + timedelta(minutes=30, seconds=1)).isoformat(),
                }
            ],
        )
        live_res = build_instrument_event_markers(
            market="NASDAQ",
            symbol="AAPL",
            execution_mode="LIVE_MARKET_LIVE",
            paper_engine=stub_engine,
            alpaca_service=live_alpaca,
        )
        assert live_res["simulated"] is False
        assert any(
            m["simulated"] is False and "BROKER_CONFIRMED" in m["verification_origin"]
            for m in live_res["markers"]
        )


class TestPaginationAndReadOnlyRoutes:
    def test_large_watchlist_pagination_560_symbols_fast_and_bounded(self) -> None:
        nse_wl = [{"symbol": f"NSESYM{i:03d}"} for i in range(300)]
        us_wl = [{"symbol": f"USSYM{i:03d}"} for i in range(260)]
        stub_engine = _StubPaperSnapshotEngine(
            {
                "NSE": {"market": "NSE", "currency": "INR", "session_state": "STOPPED", "strategy": "parallel_ensemble", "timeframe": "5m", "watchlist": nse_wl, "positions": [], "orders": [], "fills": [], "decisions": []},
                "NASDAQ": {"market": "NASDAQ", "currency": "USD", "session_state": "STOPPED", "strategy": "parallel_ensemble", "timeframe": "5m", "watchlist": us_wl, "positions": [], "orders": [], "fills": [], "decisions": []},
            }
        )
        t0 = time.perf_counter()
        p1 = build_trade_monitor_summary(
            paper_engine=stub_engine,
            page=1,
            page_size=50,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < 250.0
        assert p1["read_only"] is True
        assert p1["broker_order_api_calls"] == 0
        assert p1["counts"]["total_unfiltered"] == 560
        assert p1["pagination"]["total_pages"] == 12
        assert len(p1["items"]) == 50
        assert p1["pagination"]["has_next"] is True

        p12 = build_trade_monitor_summary(
            paper_engine=stub_engine,
            page=12,
            page_size=50,
        )
        assert len(p12["items"]) == 10
        assert p12["pagination"]["has_next"] is False

    def test_api_routes_are_strictly_get_only_and_never_substitute_sample_bars(self, tmp_path: Path) -> None:
        settings = replace(Settings(), api_token="mon-tok", database_url=f"sqlite:///{tmp_path}/mon_web.db")
        headers = {"Authorization": "Bearer mon-tok"}
        app = create_app(settings, data_dir=tmp_path)
        with TestClient(app) as client:
            # Mutating verbs MUST be rejected with 405 Method Not Allowed on monitor endpoints
            for method in ("post", "put", "delete", "patch"):
                r1 = getattr(client, method)("/api/markets/trade-monitor", headers=headers)
                r2 = getattr(client, method)("/api/markets/trade-monitor/chart", headers=headers)
                assert r1.status_code == 405
                assert r2.status_code == 405

            # Seed one simulated paper order via LiveMarketPaperEngine during regular NSE session hours
            nse_open = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)  # Thursday 11:30 IST (NSE open)
            eng: LiveMarketPaperEngine = app.state.live_paper_engine
            eng._clock = lambda: nse_open
            sub_res = eng.submit_order(
                market="NSE",
                symbol="RELIANCE",
                side="BUY",
                qty=4.0,
                order_type="MARKET",
                stop_loss=1395.0,
                take_profit=1465.0,
                quote_override={
                    "last": 1420.0,
                    "bid": 1419.95,
                    "ask": 1420.05,
                    "volume": 50_000.0,
                    "timestamp": nse_open.isoformat(),
                    "source": "unit_test_quote",
                },
            )
            assert sub_res["ok"] is True

            mon = client.get("/api/markets/trade-monitor?market=NSE", headers=headers)
            assert mon.status_code == 200
            mbody = mon.json()
            assert mbody["read_only"] is True
            assert mbody["broker_order_api_calls"] == 0
            rel = next(x for x in mbody["items"] if x["symbol"] == "RELIANCE")
            assert rel["simulated"] is True
            assert "SIMULATED" in rel["verification_origin"]
            assert rel["actively_trading"] is False  # Session is still STOPPED -> NOT TRADING!

            chart = client.get(
                "/api/markets/trade-monitor/chart?market=NSE&symbol=RELIANCE&timeframe=5m&execution_mode=LIVE_MARKET_PAPER",
                headers=headers,
            )
            assert chart.status_code == 200
            cbody = chart.json()
            assert cbody["read_only"] is True
            assert cbody["broker_order_api_calls"] == 0
            # Without Upstox credentials in test sandbox, bars MUST be empty (never fake sample candles!)
            assert cbody["bars"] == []
            # Yet the simulated ledger markers & bracket lines remain truthfully inspectable
            assert len(cbody["markers"]) >= 3  # COMPLETED_FILL + STOP_LOSS + TAKE_PROFIT
            assert any(m["marker_type"] == "COMPLETED_FILL" and m["simulated"] is True for m in cbody["markers"])
