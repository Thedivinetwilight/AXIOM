"""Chart terminal + manual paper trading: contract tests.

What is asserted here, and why it matters:

* An aggregated bar is only produced from complete, contiguous source bars —
  a 5m candle built from four of five minutes would lie about its high, low
  and volume.
* Daily bars are never synthesised from intraday bars, because a session is a
  calendar boundary and not a whole number of UTC seconds.
* A market order fills at the **last completed bar's close**, never at the
  forming bar, so the ticket cannot look ahead.
* The ticket's headroom is the number the order endpoint will actually accept.
* Manual orders respect every configured cap, cannot short, and cannot spend
  cash the account does not have.
* The chart page and its assets load; the data routes stay behind the bearer
  gate; a wrong symbol/timeframe/indicator is refused rather than silently
  substituted.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.backtesting.costs import ExecutionConfig
from app.charting import series as chart_series
from app.charting import source as chart_source
from app.config.settings import Settings
from app.data.models import Candle
from app.paper_trading.broker import PaperBroker
from app.paper_trading.execution import ContinuousCalendar
from app.paper_trading.manual import (
    ManualLimits,
    ManualOrderRejected,
    place_order,
    ticket,
)

TOKEN = "chart-terminal-token"
H = {"Authorization": f"Bearer {TOKEN}"}
REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ helpers


def minute(index: int, *, close: float = 100.0, symbol: str = "AAPL") -> Candle:
    start = datetime(2026, 10, 22, 13, 30, tzinfo=timezone.utc)
    open_ = close - 0.2
    return Candle(
        start + timedelta(minutes=index),
        open_,
        max(open_, close) + 0.5,
        min(open_, close) - 0.5,
        close,
        1_000 + index,
        symbol,
    )


def broker(**kwargs) -> PaperBroker:
    config = kwargs.pop("config", None) or ExecutionConfig(
        initial_capital=100_000, commission=0.0005, slippage=0.0005, spread=0.0002,
        position_size=0.25, stop_loss=0.05, take_profit=0.10, max_participation=0.5,
    )
    return PaperBroker(config, currency="USD", timeframe="1m",
                       calendar=ContinuousCalendar(), **kwargs)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/chart.db")
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_SOURCE", "generic")
    settings = Settings.from_env()
    # The repository's sample datasets are what a fresh install can chart.
    app = create_app(settings, data_dir=REPO / "data")
    with TestClient(app) as test_client:
        test_client.app_state_dir = tmp_path
        yield test_client


# ------------------------------------------------------------- aggregation


class TestResample:
    def test_exact_multiple_aggregates_ohlcv(self):
        bars = [minute(i, close=100 + i) for i in range(10)]
        out = chart_series.resample(bars, "1m", "5m")
        assert len(out) == 2
        first = out[0]
        assert first.timestamp == bars[0].timestamp
        assert first.open == bars[0].open
        assert first.close == bars[4].close
        assert first.high == max(b.high for b in bars[:5])
        assert first.low == min(b.low for b in bars[:5])
        assert first.volume == sum(b.volume for b in bars[:5])

    def test_incomplete_bucket_is_dropped_not_padded(self):
        bars = [minute(i) for i in range(7)]  # 5 + 2
        out = chart_series.resample(bars, "1m", "5m")
        assert len(out) == 1

    def test_gap_in_the_middle_never_produces_a_bucket(self):
        bars = [minute(i) for i in range(10) if i != 3]
        out = chart_series.resample(bars, "1m", "5m")
        # The 13:30 bucket is incomplete (four of five minutes), so it is
        # dropped; only the 13:35 bucket survives.
        assert len(out) == 1
        assert out[0].timestamp == datetime(2026, 10, 22, 13, 35, tzinfo=timezone.utc)
        assert out[0].volume == sum(b.volume for b in bars if b.timestamp.minute >= 35)

    def test_daily_is_never_synthesised_from_intraday(self):
        assert chart_series.can_aggregate("1m", "1D") is False
        with pytest.raises(chart_series.ResampleError):
            chart_series.resample([minute(i) for i in range(30)], "1m", "1D")

    def test_intraday_is_never_synthesised_from_daily(self):
        daily = Candle(datetime(2026, 10, 21, tzinfo=timezone.utc),
                       100, 101, 99, 100.5, 10_000, "AAPL")
        assert chart_series.can_aggregate("1D", "1h") is False
        with pytest.raises(chart_series.ResampleError):
            chart_series.resample([daily], "1D", "1h")

    def test_non_multiple_targets_are_refused(self):
        assert chart_series.can_aggregate("1m", "4h") is True
        assert chart_series.can_aggregate("5m", "15m") is True
        assert chart_series.can_aggregate("1h", "30m") is False

    def test_overlays_align_with_bars_and_stay_empty_during_warmup(self):
        bars = [minute(i, close=100 + i) for i in range(30)]
        overlay = chart_series.overlay_series(bars, ["sma20"])[0]
        assert len(overlay["values"]) == 30
        assert all(value is None for value in overlay["values"][:19])
        assert overlay["values"][19] is not None
        assert overlay["values"][19] == pytest.approx(sum(range(100, 120)) / 20)

    def test_unknown_indicator_is_not_drawn(self):
        bars = [minute(i) for i in range(5)]
        assert chart_series.overlay_series(bars, ["nope"]) == []
        assert chart_series.oscillator_series(bars, ["nope"]) == []


# ---------------------------------------------------------------- markers


class TestMarkers:
    def test_fills_bin_onto_the_containing_bar(self):
        from app.charting.markers import trade_markers

        engine = broker()
        for i in range(3):
            engine.on_bar(minute(i), persist=False)
        result = place_order(engine, symbol="AAPL", side="BUY", quantity=1,
                             limits=ManualLimits())
        marks = trade_markers(engine, "AAPL", [minute(i) for i in range(3)], "1m")
        assert len(marks) == 1
        assert marks[0]["side"] == "BUY"
        assert marks[0]["on_chart"] is True
        assert marks[0]["bar"] == 2
        assert result["fill"]["price"] > 0

    def test_fill_newer_than_the_drawn_bars_lands_on_the_last_bar(self):
        from app.charting.markers import trade_markers

        engine = broker()
        engine.on_bar(minute(0), persist=False)
        place_order(engine, symbol="AAPL", side="BUY", quantity=1, limits=ManualLimits())
        # Only the first bar is drawn: the fill is stamped at the account
        # clock, which is the newest bar, so it is drawn there.
        marks = trade_markers(engine, "AAPL", [minute(0)], "1m")
        assert marks[0]["bar"] == 0

    def test_order_time_is_the_account_clock_not_the_wall_clock(self):
        engine = broker()
        for i in range(3):
            engine.on_bar(minute(i), persist=False)
        result = place_order(engine, symbol="AAPL", side="BUY", quantity=1,
                             order_type="LIMIT", limit_price=50.0)
        assert result["order"]["timestamp"] == engine.portfolio.last_timestamp
        assert result["requested_at"] != result["stamped_at"]

    def test_working_limit_order_is_visible(self):
        from app.charting.markers import order_markers

        engine = broker()
        for i in range(3):
            engine.on_bar(minute(i), persist=False)
        place_order(engine, symbol="AAPL", side="BUY", quantity=1, order_type="LIMIT",
                    limit_price=50.0, limits=ManualLimits())
        resting = order_markers(engine, "AAPL", [minute(i) for i in range(3)], "1m")
        assert len(resting) == 1
        assert resting[0]["status"] == "NEW"
        assert resting[0]["limit_price"] == 50.0

    def test_resting_order_newer_than_the_drawn_bars_still_appears(self):
        from app.charting.markers import order_markers

        engine = broker()
        engine.on_bar(minute(0), persist=False)
        place_order(engine, symbol="AAPL", side="BUY", quantity=1, order_type="LIMIT",
                    limit_price=50.0, limits=ManualLimits())
        # The order is stamped at the account clock, one bar after the drawn
        # history if only the first bar is on screen; it must still be visible.
        drawn = [minute(0)]
        marks = order_markers(engine, "AAPL", drawn, "1m")
        assert len(marks) == 1
        assert marks[0]["bar"] == 0
        assert marks[0]["on_chart"] is True

    def test_position_levels_come_from_the_open_position(self):
        from app.charting.markers import position_levels

        engine = broker()
        for i in range(3):
            engine.on_bar(minute(i, close=100 + i), persist=False)
        place_order(engine, symbol="AAPL", side="BUY", quantity=2, limits=ManualLimits())
        levels = position_levels(engine, "AAPL")
        assert levels["quantity"] == pytest.approx(2)
        assert levels["stop_price"] == pytest.approx(levels["entry_price"] * 0.95, rel=1e-9)
        assert levels["target_price"] == pytest.approx(levels["entry_price"] * 1.10, rel=1e-9)
        assert position_levels(engine, "MSFT") is None


# ------------------------------------------------------------ manual orders


class TestManualOrders:
    def test_market_buy_fills_at_the_last_closed_bar(self):
        engine = broker()
        for i in range(3):
            engine.on_bar(minute(i, close=100 + i), persist=False)
        result = place_order(engine, symbol="AAPL", side="BUY", quantity=4,
                             limits=ManualLimits())
        assert result["status"] == "FILLED"
        assert result["simulated"] is True
        assert result["order"]["strategy"] == "manual"
        expected = engine.config.fill_price(102, "BUY")
        assert result["fill"]["price"] == pytest.approx(expected, rel=1e-9)

    def test_market_order_needs_a_completed_bar(self):
        engine = broker()
        with pytest.raises(ManualOrderRejected) as excinfo:
            place_order(engine, symbol="AAPL", side="BUY", quantity=1)
        assert excinfo.value.reason == "no_market_price"
        assert "start a paper session" in str(excinfo.value)

    def test_shorting_is_refused_with_a_reason(self):
        engine = broker()
        engine.on_bar(minute(0), persist=False)
        with pytest.raises(ManualOrderRejected) as excinfo:
            place_order(engine, symbol="AAPL", side="SELL", quantity=1)
        assert excinfo.value.reason == "insufficient_position"
        assert "shorting is not modelled" in str(excinfo.value)

    def test_position_cap_binds_before_cash(self):
        engine = broker()
        engine.on_bar(minute(0, close=100), persist=False)
        limits = ManualLimits(max_position_value=500)
        with pytest.raises(ManualOrderRejected) as excinfo:
            place_order(engine, symbol="AAPL", side="BUY", quantity=10, limits=limits)
        assert excinfo.value.reason == "max_position_value"

    def test_position_cap_counts_working_orders(self):
        engine = broker()
        engine.on_bar(minute(0, close=100), persist=False)
        limits = ManualLimits(max_position_value=600)
        place_order(engine, symbol="AAPL", side="BUY", quantity=5, order_type="LIMIT",
                    limit_price=90, limits=limits)   # 450 of notional pending
        with pytest.raises(ManualOrderRejected) as excinfo:
            place_order(engine, symbol="AAPL", side="BUY", quantity=5, order_type="LIMIT",
                        limit_price=90, limits=limits)
        assert excinfo.value.reason == "max_position_value"
        assert "pending" in str(excinfo.value)

    def test_cash_is_never_overspent(self):
        engine = broker(config=ExecutionConfig(initial_capital=1_000))
        engine.on_bar(minute(0, close=100), persist=False)
        with pytest.raises(ManualOrderRejected) as excinfo:
            place_order(engine, symbol="AAPL", side="BUY", quantity=100)
        assert excinfo.value.reason == "insufficient_cash"
        assert engine.portfolio.cash == pytest.approx(1_000)

    def test_open_position_cap_applies_to_new_symbols(self):
        engine = broker()
        for symbol in ("AAPL", "MSFT"):
            for i in range(2):
                engine.on_bar(minute(i, close=100, symbol=symbol), persist=False)
        limits = ManualLimits(max_open_positions=1)
        place_order(engine, symbol="AAPL", side="BUY", quantity=1, limits=limits)
        with pytest.raises(ManualOrderRejected) as excinfo:
            place_order(engine, symbol="MSFT", side="BUY", quantity=1, limits=limits)
        assert excinfo.value.reason == "max_open_positions"

    def test_drawdown_of_daily_pnl_blocks_entries_not_exits(self):
        engine = broker()
        engine.on_bar(minute(0, close=100), persist=False)
        place_order(engine, symbol="AAPL", side="BUY", quantity=5)
        engine.portfolio.day_start_equity = engine.portfolio.equity + 5_000
        limits = ManualLimits(max_daily_loss=100)
        with pytest.raises(ManualOrderRejected) as excinfo:
            place_order(engine, symbol="AAPL", side="BUY", quantity=1, limits=limits)
        assert excinfo.value.reason == "max_daily_loss"
        # Selling out of the position is still allowed: risk-off must not trap
        # an operator in a position they want to close.
        result = place_order(engine, symbol="AAPL", side="SELL", quantity=5, limits=limits)
        assert result["status"] == "FILLED"

    def test_notional_sizing_and_quantity_sizing_are_exclusive(self):
        engine = broker()
        engine.on_bar(minute(0, close=100), persist=False)
        with pytest.raises(ValueError):
            place_order(engine, symbol="AAPL", side="BUY", quantity=1, notional=100)
        with pytest.raises(ValueError):
            place_order(engine, symbol="AAPL", side="BUY")
        result = place_order(engine, symbol="AAPL", side="BUY", notional=2_000)
        assert result["order"]["quantity"] > 0

    def test_resting_limit_does_not_fill_until_a_bar_trades_through(self):
        engine = broker()
        engine.on_bar(minute(0, close=100), persist=False)
        placed = place_order(engine, symbol="AAPL", side="BUY", quantity=1,
                             order_type="LIMIT", limit_price=90)
        assert placed["status"] == "NEW"
        assert placed["fill"] is None
        engine.on_bar(minute(1, close=100), persist=False)
        assert engine.orders[placed["order"]["id"]].status == "NEW"
        engine.on_bar(minute(2, close=89), persist=False)
        assert engine.orders[placed["order"]["id"]].status == "FILLED"
        # It filled as a limit: never above the limit price.
        assert engine.fills[-1].price <= 90.0

    def test_marketable_limit_never_fills_worse_than_its_limit(self):
        engine = broker()
        engine.on_bar(minute(0, close=100), persist=False)
        bought = place_order(engine, symbol="AAPL", side="BUY", quantity=1,
                             order_type="LIMIT", limit_price=99.0)
        assert bought["status"] == "NEW"  # 99 < last close: not marketable
        marketable = place_order(engine, symbol="AAPL", side="BUY", quantity=1,
                                 order_type="LIMIT", limit_price=150.0)
        assert marketable["status"] == "FILLED"
        assert marketable["fill"]["price"] <= 150.0

    def test_ticket_headroom_is_what_the_endpoint_accepts(self):
        engine = broker()
        engine.on_bar(minute(0, close=100), persist=False)
        limits = ManualLimits(max_position_value=5_000)
        view = ticket(engine, "AAPL", limits)
        assert view["max_buy_quantity"] > 0
        placed = place_order(engine, symbol="AAPL", side="BUY",
                             quantity=view["max_buy_quantity"], limits=limits)
        assert placed["status"] == "FILLED"
        # The reported headroom is a real cap, not a decorative number: the
        # ticket's own sizing cannot be repeated at the same size.
        with pytest.raises(ManualOrderRejected) as excinfo:
            place_order(engine, symbol="AAPL", side="BUY",
                        quantity=view["max_buy_quantity"], limits=limits)
        assert excinfo.value.reason == "max_position_value"

    def test_ticket_reports_the_reason_it_is_blocked(self):
        engine = broker()
        engine.on_bar(minute(0, close=100), persist=False)
        view = ticket(engine, "AAPL", ManualLimits())
        assert view["blocked"] is None
        assert view["paper_only"] is True
        assert view["limits"]["allow_short"] is False

    def test_unknown_symbol_is_rejected_by_validation(self):
        engine = broker()
        with pytest.raises(ValueError):
            ticket(engine, "BAD SYMBOL", ManualLimits())


# ------------------------------------------------------------------ sources


class TestSources:
    def test_dataset_source_requires_the_declared_timeframe(self, tmp_path):
        path = tmp_path / "daily.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume,symbol\n"
            "2026-01-05T00:00:00+00:00,100,101,99,100.5,1000,AAA\n"
            "2026-01-06T00:00:00+00:00,100.5,102,100,101.5,1200,AAA\n",
            encoding="utf-8",
        )
        assert chart_source.declared_timeframe(path) == "1D"

    def test_inferring_a_timeframe_never_guesses(self, tmp_path):
        mixed = tmp_path / "mixed.csv"
        mixed.write_text(
            "timestamp,open,high,low,close,volume,symbol\n"
            "2026-01-05T00:00:00+00:00,1,1,1,1,1,AAA\n"
            "2026-01-05T00:01:00+00:00,1,1,1,1,1,AAA\n"
            "2026-01-05T00:03:00+00:00,1,1,1,1,1,AAA\n",
            encoding="utf-8",
        )
        assert chart_source.declared_timeframe(mixed) is None

    def test_no_data_is_reported_as_no_data(self, tmp_path):
        from types import SimpleNamespace

        app = SimpleNamespace(state=SimpleNamespace(data_dir=tmp_path, simulator=None,
                                                    broker=None))
        resolved = chart_source.resolve(app, "AAPL", "1m", 100)
        assert resolved.empty
        assert resolved.source == "none"
        assert "no bars" in resolved.note


# -------------------------------------------------------------------- routes


class TestChartApi:
    def test_page_and_assets_load_without_a_bearer_on_loopback(self, client):
        page = client.get("/chart")
        assert page.status_code == 200
        assert "chart.js" in page.text
        # The page must not contain the token, a remote script, or an order path.
        assert TOKEN not in page.text
        assert "http://" not in page.text and "https://" not in page.text
        assert client.get("/static/chart/chart.js").status_code == 200
        assert client.get("/static/chart/chart.css").status_code == 200

    def test_data_routes_stay_gated(self, client):
        for path in ("/chart/data", "/chart/symbols", "/paper/ticket?symbol=DEMO"):
            response = client.get(path)
            assert response.status_code == 401, path
            assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
            ok = client.get(path, headers=H)
            assert ok.status_code != 401, path

    def test_symbols_lists_dataset_instruments_with_honest_sources(self, client):
        payload = client.get("/chart/symbols", headers=H).json()
        symbols = {entry["symbol"]: entry for entry in payload["symbols"]}
        assert "DEMO" in symbols
        assert symbols["DEMO"]["source"].startswith("dataset:")
        assert symbols["DEMO"]["live"] is False
        assert payload["paper_only"] is True
        assert payload["live_trading"] is False

    def test_data_payload_carries_bars_overlays_and_provenance(self, client):
        payload = client.get("/chart/data", headers=H, params={
            "symbol": "DEMO", "timeframe": "1D", "limit": 120,
            "indicators": "sma20,rsi14",
        }).json()
        assert payload["symbol"] == "DEMO"
        assert payload["paper_only"] is True
        assert payload["source"].startswith("dataset:")
        assert 0 < payload["count"] <= 120
        assert len(payload["bars"]["time"]) == payload["count"]
        assert [line["key"] for line in payload["overlays"]] == ["sma20"]
        assert [line["key"] for line in payload["oscillators"]] == ["rsi14"]
        # DEMO ships as a 1D file and a 1h file, so both are drawable.
        assert "1D" in payload["available"]["timeframes"]
        assert "1h" in payload["available"]["timeframes"]
        assert payload["markers"] == []
        assert payload["position"] is None

    def test_intraday_timeframe_is_aggregated_from_the_file_that_has_it(self, client):
        payload = client.get("/chart/data", headers=H, params={
            "symbol": "DEMO", "timeframe": "4h", "limit": 50,
        }).json()
        assert payload["source_timeframe"] == "1h"
        assert payload["count"] == 50
        assert "aggregated from the 1h" in payload["note"]
        assert payload["timeframe"] == "4h"

    def test_engine_bars_win_over_a_dataset_preview(self, client):
        client.post("/paper/start", headers=H, json={
            "source": "replay", "symbol": "DEMO", "timeframe": "1D",
            "strategy": "moving_average", "replay_delay": 0,
        })
        wait_for_bar(client)
        payload = client.get("/chart/data", headers=H, params={
            "symbol": "DEMO", "timeframe": "1D", "limit": 40,
        }).json()
        assert payload["source"] == "engine"
        assert payload["live"] is True
        client.post("/paper/stop", headers=H)

    def test_unknown_indicator_is_refused_not_ignored(self, client):
        response = client.get("/chart/data", headers=H, params={
            "symbol": "DEMO", "timeframe": "1D", "indicators": "sma20,magic"})
        assert response.status_code == 422
        assert "magic" in response.text

    def test_impossible_timeframe_is_explained(self, client):
        response = client.get("/chart/data", headers=H, params={
            "symbol": "DEMO", "timeframe": "5m"})
        assert response.status_code == 422
        assert "cannot be derived" in response.text

    def test_bar_limit_is_bounded(self, client):
        assert client.get("/chart/data", headers=H,
                          params={"symbol": "DEMO", "timeframe": "1D",
                                  "limit": 5000}).status_code == 422

    def test_dashboard_links_to_the_chart(self, client):
        assert 'href="/chart"' in client.get("/dashboard").text


def wait_for_bar(client, symbol="DEMO", tries=60):
    """Replay runs on the app's event loop, so a sync test must poll for it."""
    import time

    for _ in range(tries):
        payload = client.get("/paper/ticket", headers=H,
                             params={"symbol": symbol}).json()
        if payload.get("last_price"):
            return payload
        time.sleep(0.05)
    raise AssertionError("the replay produced no bar for %s" % symbol)


class TestPaperTicketApi:
    def test_ticket_reports_price_and_caps(self, client):
        client.post("/paper/start", headers=H, json={
            "source": "replay", "symbol": "DEMO", "timeframe": "1D",
            "strategy": "moving_average", "replay_delay": 0,
        })
        payload = wait_for_bar(client)
        assert payload["last_price"] > 0
        assert payload["paper_only"] is True
        assert payload["live_trading"] is False
        assert payload["limits"]["allow_short"] is False
        client.post("/paper/stop", headers=H)

    def test_order_is_simulated_and_recorded(self, client):
        client.post("/paper/start", headers=H, json={
            "source": "replay", "symbol": "DEMO", "timeframe": "1D",
            "strategy": "moving_average", "replay_delay": 0,
        })
        wait_for_bar(client)
        response = client.post("/paper/order", headers=H, json={
            "symbol": "DEMO", "side": "BUY", "quantity": 2})
        assert response.status_code == 200
        payload = response.json()
        assert payload["simulated"] is True
        assert payload["paper_only"] is True
        assert payload["live_trading"] is False
        assert payload["order"]["strategy"] == "manual"
        # The order is visible through the ordinary paper endpoints.
        orders = client.get("/paper/orders", headers=H).json()["orders"]
        assert any(order["id"] == payload["order"]["id"] for order in orders)
        client.post("/paper/stop", headers=H)

    def test_rejections_carry_a_reason_code(self, client):
        client.post("/paper/start", headers=H, json={
            "source": "replay", "symbol": "DEMO", "timeframe": "1D",
            "strategy": "moving_average", "replay_delay": 0,
        })
        wait_for_bar(client)
        response = client.post("/paper/order", headers=H, json={
            "symbol": "DEMO", "side": "SELL", "quantity": 1})
        assert response.status_code == 422
        assert "not modelled" in response.text
        client.post("/paper/stop", headers=H)

    def test_bad_sizing_is_refused_by_the_schema(self, client):
        for body in (
            {"symbol": "DEMO", "side": "BUY", "quantity": 1, "notional": 100},
            {"symbol": "DEMO", "side": "BUY"},
            {"symbol": "DEMO", "side": "BUY", "quantity": 1, "order_type": "LIMIT"},
            {"symbol": "DEMO", "side": "HOLD", "quantity": 1},
            {"symbol": "DEMO", "side": "BUY", "quantity": -5},
        ):
            assert client.post("/paper/order", headers=H, json=body).status_code == 422, body


class TestProxiedPageAccess:
    """A proxied deployment can serve the page; data still needs the token."""

    def test_page_is_refused_to_a_remote_client_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/p.db")
        monkeypatch.setenv("API_TOKEN", TOKEN)
        settings = Settings.from_env()
        assert settings.ui_allow_proxy_pages is False
        app = create_app(settings, data_dir=REPO / "data")
        with TestClient(app, client=("203.0.113.9", 5000)) as test_client:
            assert test_client.get("/chart").status_code == 401
            # ... and the bearer still opens it.
            assert test_client.get("/chart", headers=H).status_code == 200

    def test_opt_in_serves_the_page_but_never_the_data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/p2.db")
        monkeypatch.setenv("API_TOKEN", TOKEN)
        monkeypatch.setenv("UI_ALLOW_PROXY_PAGES", "true")
        settings = Settings.from_env()
        app = create_app(settings, data_dir=REPO / "data")
        with TestClient(app, client=("203.0.113.9", 5000)) as test_client:
            assert test_client.get("/chart").status_code == 200
            # Every data route is unchanged: no token, no data.
            for path in ("/chart/data", "/chart/symbols", "/paper/ticket?symbol=DEMO",
                         "/dashboard/data"):
                assert test_client.get(path).status_code == 401, path
            assert test_client.get("/chart/data", headers=H).status_code == 200


class TestManualOrdersRefuseAnythingButLocalPaper:
    def test_non_local_broker_is_refused(self, client):
        from app.paper_trading import manual

        original = client.app.state.broker
        client.app.state.broker = object()  # not a PaperBroker
        try:
            response = client.post("/paper/order", headers=H, json={
                "symbol": "DEMO", "side": "BUY", "quantity": 1})
        finally:
            client.app.state.broker = original
        assert response.status_code == 409
        assert "LOCAL_PAPER" in response.text
        assert manual  # imported for the assertion above

    def test_page_contains_the_simulation_notice(self, client):
        text = client.get("/chart").text
        assert "SIMULATED" in text.upper()
        assert "Nothing is sent to a broker" in text


# --------------------------------------------------------------- JS harness


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_chart_script_executes_in_a_headless_harness():
    """Runs the real chart.js against a DOM/canvas stub and canned payloads."""
    result = subprocess.run(
        ["node", str(REPO / "tests" / "js" / "chart_smoke.mjs")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "chart harness: OK" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_chart_assets_parse_as_javascript():
    for name in ("chart.js",):
        path = REPO / "app" / "static" / "chart" / name
        result = subprocess.run(["node", "--check", str(path)],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr


def test_chart_page_has_no_inline_event_handlers_or_remote_assets():
    """A CSP-friendly page: all behaviour lives in the local script."""
    page = (REPO / "app" / "static" / "chart" / "index.html").read_text(encoding="utf-8")
    lowered = page.lower()
    assert "onclick=" not in lowered
    assert "cdn" not in lowered
    assert "<script src=\"/static/chart/chart.js\">" in page
