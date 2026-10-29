"""US equities market data -> local paper fills -> chart terminal.

The ported US modules (:mod:`app.data.alpaca`, :mod:`app.markets.us_calendar`,
the paper entry limits) are tested on their own in
``tests/test_us_equities_paper.py``. This file tests the *join* that makes the
feature usable, and that the join cannot quietly lose its safety properties:

* an Alpaca-configured app lists its tickers on the chart before the first bar
  exists, and says exactly why the chart is empty;
* closed 1-minute bars from the read-only stream reach the engine, become local
  simulated fills, and are drawn with their markers;
* a bar outside the New York session is neither drawn nor filled;
* the chart always names its feed, so a DEMO replay can never be read as a
  market-data result;
* hand-entered orders are subject to the same paper caps as strategy entries;
* the Alpaca code path contains no order surface at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings
from app.data.models import Candle
from app.markets.us_calendar import USEquitiesCalendar
from app.paper_trading.broker import PaperBroker
from app.paper_trading.manual import ManualLimits, ManualOrderRejected, place_order
from app.paper_trading.simulator import BotState, PaperSimulator
from app.strategies import create_strategy

TOKEN = "us-integration-token"
H = {"Authorization": f"Bearer {TOKEN}"}
REPO = Path(__file__).resolve().parent.parent

# Fake, obviously-not-real values: this file never contacts Alpaca.
FAKE_KEY = "paper-only-key-not-a-credential"
FAKE_SECRET = "paper-only-secret-not-a-credential"


def us_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        data_source="alpaca",
        market="us",
        currency="USD",
        market_calendar="us-equities",
        default_timeframe="1m",
        default_symbol="AAPL",
        alpaca_symbols="AAPL,MSFT",
        alpaca_api_key=FAKE_KEY,
        alpaca_api_secret=FAKE_SECRET,
        paper_max_position_value=5_000,
        paper_max_open_positions=2,
        paper_max_daily_loss=500,
        paper_max_drawdown=0.20,
        paper_max_trades_per_day=4,
        api_token=TOKEN,
        database_url=f"sqlite:///{tmp_path}/us_chart.db",
    )
    values.update(overrides)
    return Settings(**values)


class FakeAlpacaStream:
    """The shape of a live Alpaca bar stream, without a socket."""

    stale_after_seconds = None  # deterministic test clocks are not "stale"
    status = "CONNECTED"
    last_message = None

    def __init__(self, symbols):
        self.symbols = list(symbols)

    def metrics(self):
        return {"provider": "alpaca", "feed_mode": "iex",
                "connection_status": self.status, "paper_mode": True}

    async def close(self):
        pass


def us_broker(risk_limits: dict | None = None, **config) -> PaperBroker:
    params = dict(initial_capital=100_000, commission=0.0005, slippage=0.0005,
                  spread=0.0002, position_size=0.25, stop_loss=None,
                  take_profit=None, max_participation=0.5)
    params.update(config)
    return PaperBroker(
        ExecutionConfig(**params),
        currency="USD",
        timeframe="1m",
        calendar=USEquitiesCalendar(),
        risk_limits=risk_limits,
        restore=False,
    )


def engine(symbols=("AAPL",), risk_limits: dict | None = None):
    """A paper simulator wired to a fake Alpaca stream, ready for process_bar."""
    sim = PaperSimulator(us_broker(risk_limits))
    sim.provider = FakeAlpacaStream(symbols)
    sim.symbols = list(symbols)
    sim.strategy = create_strategy("moving_average", {"fast_period": 2, "slow_period": 3})
    sim.state = BotState.RUNNING
    return sim


def minute(index: int, price: float, symbol: str = "AAPL") -> Candle:
    """A 1-minute bar at 10:30 New York (14:30 UTC) on a regular session day."""
    start = datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)
    return Candle(start + timedelta(minutes=index), price, price * 1.001,
                  price * 0.999, price, 5_000, symbol)


@pytest.fixture()
def us_client(tmp_path):
    app = create_app(us_settings(tmp_path), data_dir=tmp_path / "data")
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ the join


class TestAlpacaSymbolsOnTheChart:
    def test_configured_tickers_are_listed_before_the_first_bar(self, us_client):
        body = us_client.get("/chart/symbols", headers=H).json()
        listed = {entry["symbol"]: entry for entry in body["symbols"]}
        assert set(listed) == {"AAPL", "MSFT"}
        for entry in listed.values():
            assert entry["data_feed"] == "alpaca"
            assert "ALPACA US MARKET DATA" in entry["data_feed_label"]
            # Nothing is running yet, and the label says so rather than
            # implying a live feed.
            assert "NO SESSION RUNNING" in entry["data_feed_label"]
            assert entry["live"] is False
            assert entry["timeframes"][0] == "1m"
        assert body["default"] == "AAPL"
        assert body["paper_only"] is True and body["live_trading"] is False

    def test_empty_chart_says_what_will_fill_it(self, us_client):
        body = us_client.get(
            "/chart/data?symbol=AAPL&timeframe=1m", headers=H
        ).json()
        assert body["count"] == 0
        assert body["source"] == "none"
        assert body["data_feed"] == "alpaca"
        assert body["market_data"] is False
        assert "Alpaca" in body["note"] and "session" in body["note"]

    def test_unknown_ticker_is_refused_not_substituted(self, us_client):
        response = us_client.get(
            "/chart/data?symbol=TSLA&timeframe=1m", headers=H
        )
        # Not configured, so there is nothing to draw and the request fails
        # loudly instead of borrowing another instrument's bars.
        assert response.status_code == 422

    def test_data_routes_still_require_the_bearer(self, us_client):
        assert us_client.get("/chart/symbols").status_code in {401, 503}


class TestUsBarsBecomeSimulatedFills:
    def test_session_bars_are_drawn_with_their_fill_markers(self, tmp_path):
        settings = us_settings(tmp_path)
        app = create_app(settings, data_dir=tmp_path / "data")
        with TestClient(app) as client:
            sim = engine(risk_limits=settings.paper_risk_limits())
            app.state.broker, app.state.simulator = sim.broker, sim

            # Flat, then up (fast crosses above slow -> BUY), then down (-> SELL).
            path = [100.0] * 12 + [100.4] * 6 + [99.0] * 8
            for index, price in enumerate(path):
                sim.process_bar(minute(index, price))

            fills = sim.broker.fills
            assert [fill.side for fill in fills] == ["BUY", "SELL"]
            assert all(fill.reason == "signal" for fill in fills)

            body = client.get(
                "/chart/data?symbol=AAPL&timeframe=1m", headers=H
            ).json()
            assert body["source"] == "engine"
            assert body["data_feed"] == "alpaca"
            assert body["market_data"] is True
            assert "ALPACA US MARKET DATA" in body["data_feed_label"]
            assert body["count"] == len(path)
            assert body["currency"] == "USD"
            sides = [marker["side"] for marker in body["markers"]]
            assert sides == ["BUY", "SELL"]
            assert all(marker["on_chart"] for marker in body["markers"])
            # The New York calendar, not the browser clock, decides the state.
            assert body["session"]["market_timezone"] == "America/New_York"
            assert body["session"]["market_status"] in {
                "OPEN", "PRE_OPEN", "CLOSED"
            }
            assert body["session"]["provider"] == "alpaca"

    def test_bars_outside_the_session_are_not_drawn_or_filled(self):
        sim = engine()
        for index in range(4):
            sim.process_bar(minute(index, 100.0 + index))
        drawn = len(sim.chart_bars["AAPL"])
        assert drawn == 4

        # 17:30 New York: the regular session has closed.
        after_hours = datetime(2026, 10, 21, 21, 30, tzinfo=timezone.utc)
        sim.process_bar(Candle(after_hours, 120, 121, 119, 120, 1_000, "AAPL"))
        assert len(sim.chart_bars["AAPL"]) == drawn
        assert sim.broker.last_bars["AAPL"].startswith("2026-10-21T14:33")
        assert not any(order.symbol == "AAPL" and order.status == "NEW"
                       for order in sim.broker.orders.values())
        assert all(fill.price < 120 for fill in sim.broker.fills)


class TestDemoIsNeverMistakenForMarketData:
    def test_replay_chart_is_labelled_as_sample_data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/demo.db")
        monkeypatch.setenv("API_TOKEN", TOKEN)
        monkeypatch.setenv("DATA_SOURCE", "replay")
        app = create_app(Settings.from_env(), data_dir=REPO / "data")
        with TestClient(app) as client:
            body = client.get(
                "/chart/data?symbol=DEMO&timeframe=1D", headers=H
            ).json()
            assert body["count"] > 0
            assert body["data_feed"] == "replay"
            assert body["market_data"] is False
            assert "NOT MARKET DATA" in body["data_feed_label"]

    def test_demo_dataset_is_not_relabelled_by_an_alpaca_account(self, tmp_path):
        """A CSV preview stays sample data even when DATA_SOURCE=alpaca."""
        app = create_app(us_settings(tmp_path), data_dir=REPO / "data")
        with TestClient(app) as client:
            body = client.get("/chart/symbols", headers=H).json()
            demos = {entry["symbol"]: entry for entry in body["symbols"]}
            for symbol in ("DEMO", "ALT"):
                assert demos[symbol]["data_feed"] in {"dataset", "replay"}
                assert "ALPACA" not in demos[symbol]["data_feed_label"]
                assert "NOT MARKET DATA" in demos[symbol]["data_feed_label"]
            payload = client.get(
                "/chart/data?symbol=DEMO&timeframe=1D", headers=H
            ).json()
            assert payload["data_feed"] in {"dataset", "replay"}
            assert payload["market_data"] is False
            # A CSV preview is not a US session, so no New York state is claimed.
            assert payload["session"].get("market_status") is None
            assert payload["session"].get("market_timezone") is None

    def test_dataset_preview_is_labelled_as_a_local_csv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/demo2.db")
        monkeypatch.setenv("API_TOKEN", TOKEN)
        monkeypatch.setenv("DATA_SOURCE", "generic")
        app = create_app(Settings.from_env(), data_dir=REPO / "data")
        with TestClient(app) as client:
            body = client.get(
                "/chart/data?symbol=ALT&timeframe=1D", headers=H
            ).json()
            assert body["source"].startswith("dataset:")
            assert body["data_feed"] == "dataset"
            assert body["market_data"] is False
            assert "NOT MARKET DATA" in body["data_feed_label"]


class TestPaperCapsApplyToHandEnteredOrders:
    def test_daily_loss_halt_blocks_a_manual_buy(self):
        broker = us_broker({"max_daily_loss": 100})
        broker.portfolio.marks["AAPL"] = 100.0
        broker.portfolio.cash = 99_800.0
        stamp = "2026-10-21T14:31:00+00:00"
        broker.portfolio.snapshot(stamp, "USD")
        with pytest.raises(ManualOrderRejected) as raised:
            place_order(broker, symbol="AAPL", side="BUY", quantity=1, timestamp=
                        datetime(2026, 10, 21, 14, 31, tzinfo=timezone.utc))
        assert raised.value.reason == "paper_max_daily_loss"

    def test_open_position_cap_blocks_a_second_symbol(self):
        broker = us_broker({"max_open_positions": 1})
        broker.portfolio.marks.update({"AAPL": 100.0, "MSFT": 100.0})
        stamp = datetime(2026, 10, 21, 14, 31, tzinfo=timezone.utc)
        place_order(broker, symbol="AAPL", side="BUY", quantity=1, timestamp=stamp)
        with pytest.raises(ManualOrderRejected) as raised:
            place_order(broker, symbol="MSFT", side="BUY", quantity=1, timestamp=stamp)
        assert raised.value.reason == "paper_max_open_positions"

    def test_position_value_cap_bounds_a_manual_buy(self):
        broker = us_broker({"max_position_value": 1_000})
        broker.portfolio.marks["AAPL"] = 100.0
        stamp = datetime(2026, 10, 21, 14, 31, tzinfo=timezone.utc)
        # The broker's own cap binds even when the caller passes no ManualLimits,
        # because an immediately-filled market order never reaches on_bar.
        with pytest.raises(ManualOrderRejected) as raised:
            place_order(broker, symbol="AAPL", side="BUY", quantity=50, timestamp=stamp)
        assert raised.value.reason == "paper_max_position_value"
        assert not broker.orders

    def test_manual_limits_never_relax_a_stricter_cap(self, tmp_path):
        settings = us_settings(tmp_path, max_position_value=250.0,
                               paper_max_position_value=5_000.0)
        limits = ManualLimits.from_settings(settings)
        assert limits.max_position_value == 250.0
        assert limits.max_drawdown == 0.20

        other = us_settings(tmp_path, max_position_value=0.0,
                            paper_max_position_value=750.0)
        assert ManualLimits.from_settings(other).max_position_value == 750.0

    def test_strategy_entry_and_manual_order_share_the_halt(self):
        """One account, one set of caps: the chart cannot sidestep the halt."""
        broker = us_broker({"max_trades_per_day": 1})
        broker.portfolio.marks["AAPL"] = 100.0
        stamp = datetime(2026, 10, 21, 14, 31, tzinfo=timezone.utc)
        place_order(broker, symbol="AAPL", side="BUY", quantity=1, timestamp=stamp)
        assert broker.paper_entry_block_reason("AAPL", stamp.isoformat()) == (
            "paper_max_trades_per_day"
        )


class TestUsStartUpIsPaperOnly:
    def test_alpaca_start_is_refused_on_a_non_us_account(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/inr.db")
        monkeypatch.setenv("API_TOKEN", TOKEN)
        app = create_app(Settings.from_env(), data_dir=tmp_path / "data")
        with TestClient(app) as client:
            response = client.post(
                "/paper/start",
                headers=H,
                json={"source": "alpaca", "symbol": "AAPL", "timeframe": "1D"},
            )
            assert response.status_code == 422
            assert "MARKET=us" in response.json()["detail"]
            # The refusal happens before any provider is constructed.
            assert app.state.simulator.provider is None

    def test_alpaca_requires_local_paper_and_bounded_limits(self, tmp_path):
        with pytest.raises(ValueError, match="local PAPER"):
            us_settings(tmp_path, broker="UPSTOX_SANDBOX",
                        upstox_sandbox_access_token="sandbox-token")
        with pytest.raises(ValueError, match="PAPER_MAX_POSITION_VALUE"):
            us_settings(tmp_path, paper_max_position_value=0)
        with pytest.raises(ValueError, match="PAPER_MAX_DRAWDOWN"):
            us_settings(tmp_path, paper_max_drawdown=0.0)
        with pytest.raises(ValueError, match="distinct tickers"):
            us_settings(tmp_path, alpaca_symbols="AAPL,AAPL")

    def test_live_trading_cannot_be_enabled_for_us_data(self, tmp_path):
        with pytest.raises(ValueError, match="MODE=PAPER"):
            us_settings(tmp_path, live_trading=True)


class TestNoOrderSurfaceWasPorted:
    def test_alpaca_module_only_knows_market_data_and_stream_hosts(self):
        source = (REPO / "app" / "data" / "alpaca.py").read_text(encoding="utf-8")
        assert "https://data.alpaca.markets" in source
        assert "wss://stream.data.alpaca.markets" in source
        # The trading host appears once, for the read-only listing-metadata
        # lookup (`/v1/assets/{symbol}`): symbol lookup/instrument metadata is
        # an explicit product requirement. An order endpoint, a trading SDK, or
        # any second use of the trading host would mean this port crossed from
        # market data into order routing.
        assert source.count("api.alpaca.markets") == 1
        assert '_ASSET_HOST = "https://api.alpaca.markets"' in source
        assert 'f"{_ASSET_HOST}/v1/assets/{symbol}"' in source
        for forbidden in ("/v2/orders", "/v1/orders", "TradingClient",
                          "submit_order", "cancel_order(", "place_order("):
            assert forbidden not in source

    def test_settings_carry_no_alpaca_order_configuration(self):
        names = set(Settings.__dataclass_fields__)
        # Market-data fields plus the separately gated broker fields; anything
        # else named "alpaca" would be an undocumented configuration surface.
        assert not {name for name in names if "alpaca" in name} - {
            "alpaca_api_key", "alpaca_api_secret", "alpaca_data_feed",
            "alpaca_symbols",
            "alpaca_broker_enabled", "alpaca_broker_environment",
            "alpaca_broker_api_key", "alpaca_broker_api_secret",
            "alpaca_broker_max_order_value", "alpaca_broker_max_open_positions",
            "alpaca_broker_max_daily_orders",
        }
        # Endpoint hosts are never configurable: no URL fields for Alpaca.
        assert not {name for name in names if "alpaca" in name and "url" in name}

    def test_repository_never_names_a_real_order_host(self):
        offenders = []
        metadata_module = "app/data/alpaca.py"
        broker_package = "app/alpaca_broker/"
        # The Alpaca order surface lives ONLY in the separately gated broker
        # package (its own tests prove the LIVE gate and host confinement).
        # Everywhere else — including the market-data module — the trading
        # hosts and order endpoints stay forbidden, except the one read-only
        # /v1/assets/ metadata host in app/data/alpaca.py (tested above).
        for path in (REPO / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            relative = str(path.relative_to(REPO))
            if relative.startswith(broker_package):
                continue
            if "alpaca.markets/v2/orders" in text or "alpaca.markets/v1/orders" in text:
                offenders.append(relative)
            if "paper-api.alpaca.markets" in text:
                offenders.append(relative)
            if "api.alpaca.markets" in text and relative != metadata_module:
                offenders.append(relative)
        assert offenders == []

    def test_order_surface_is_confined_to_the_gated_broker_package(self):
        # Positive confinement: the order endpoints appear exactly where the
        # gated broker client documents them — and nowhere else in app/.
        holders = []
        for path in (REPO / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "/v2/orders" in text:
                holders.append(str(path.relative_to(REPO)))
        assert holders == ["app/alpaca_broker/client.py"]
