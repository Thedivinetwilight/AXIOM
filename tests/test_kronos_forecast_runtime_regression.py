"""Regression: the NASDAQ Kronos forecast path must never fail with ``NameError``.

Path traced here (the one the operator UI drives):

    terminal.js  #ml-forecast-form   (Market = NASDAQ, "Run Validated Forecast")
      -> POST /api/model-lab/kronos/forecast        (app/api/routes/model_lab.py)
      -> app.state.nasdaq_bars_provider             (installed in app/api/server.py)
      -> _nasdaq_live_bars_lookup                   (closure inside create_app)
      -> AlpacaHistoricalProvider.get_candles       (read-only market data)

``_nasdaq_live_bars_lookup`` computed ``datetime.now(timezone.utc)`` while
``app/api/server.py`` imported neither ``datetime`` nor ``timezone``, so every
NASDAQ forecast raised ``NameError: name 'datetime' is not defined`` before an
Alpaca request was even attempted. The route reported it as "read-only bars
unavailable", which looked like a credentials/feed problem instead of a bug.

These tests drive the real closures with a fake Alpaca provider and a fake
Upstox reader: no network, no credentials, no broker session, no orders, and
Kronos model weights are never required. Live execution stays OFF.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.server as server
import app.data.alpaca as alpaca_module
import app.autonomy.upstox as upstox_module
from app.autonomy.upstox import Quote
from app.config.settings import Settings
from app.data.models import Candle

AUTH = {"Authorization": "Bearer regression-tok"}


def _make_bars(count: int = 64, *, symbol: str = "AAPL") -> list[Candle]:
    """Deterministic hourly bars; enough to satisfy the Kronos context minimum."""
    t0 = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    out: list[Candle] = []
    px = 100.0
    for i in range(count):
        nxt = px * 1.002
        out.append(
            Candle(
                timestamp=t0 + timedelta(hours=i),
                open=px,
                high=max(px, nxt) * 1.001,
                low=min(px, nxt) * 0.999,
                close=nxt,
                volume=25_000,
                symbol=symbol,
            )
        )
        px = nxt
    return out


class RecordingAlpacaProvider:
    """Stand-in for ``AlpacaHistoricalProvider``: records the call, never dials out."""

    instances: list["RecordingAlpacaProvider"] = []

    def __init__(self, key: str, secret: str, *, feed: str = "iex", timeout: float = 20.0,
                 client=None) -> None:
        self.feed = feed
        self.requests: list[dict] = []
        self.closed = False
        RecordingAlpacaProvider.instances.append(self)

    def get_candles(self, symbol: str, timeframe: str, start: datetime, end: datetime):
        self.requests.append({"symbol": symbol, "timeframe": timeframe, "start": start, "end": end})
        return _make_bars(symbol=symbol)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def alpaca_fake(monkeypatch):
    RecordingAlpacaProvider.instances = []
    monkeypatch.setattr(alpaca_module, "AlpacaHistoricalProvider", RecordingAlpacaProvider)
    return RecordingAlpacaProvider


@pytest.fixture
def app_client(tmp_path: Path):
    def _build(**overrides):
        settings = replace(
            Settings(),
            api_token="regression-tok",
            database_url=f"sqlite:///{tmp_path}/regression.db",
            alpaca_api_key="test-key-not-real",
            alpaca_api_secret="test-secret-not-real",
            alpaca_data_feed="iex",
            market_data_access_token="test-upstox-token-not-real",
            **overrides,
        )
        return TestClient(server.create_app(settings, data_dir=tmp_path))

    return _build


def test_nasdaq_forecast_window_is_utc_and_never_raises_nameerror(alpaca_fake, app_client):
    """The bar provider must return bars (previously: NameError -> no bars ever)."""
    with app_client() as web:
        bars = web.app.state.nasdaq_bars_provider("aapl", "1d")

    assert len(bars) == 64, "NASDAQ forecast provider returned no usable bars"
    provider = alpaca_fake.instances[-1]
    assert provider.closed is True, "the Alpaca client must always be closed"

    (request,) = provider.requests
    assert request["symbol"] == "AAPL", "symbol must be normalised before the request"
    assert request["timeframe"] == "1D", "Kronos 1d context must request Alpaca 1D bars"

    # The window itself is what used to explode: datetime.now(timezone.utc).
    start, end = request["start"], request["end"]
    for moment in (start, end):
        assert isinstance(moment, datetime)
        assert moment.tzinfo is not None, "Alpaca needs a timezone-aware UTC window"
        assert moment.utcoffset() == timedelta(0), "the forecast window must be UTC"
    assert end > start
    assert (end - start) == timedelta(days=240), "1D context must span the 240-day lookback"


@pytest.mark.parametrize(
    ("timeframe", "expected_tf", "lookback_days"),
    [("1d", "1D", 240), ("4h", "1h", 30), ("5m", "5m", 7)],
)
def test_nasdaq_forecast_timeframe_mapping(alpaca_fake, app_client, timeframe, expected_tf,
                                           lookback_days):
    """Alpaca has no 4h bars: 4h context is built from 1h bars, with the right lookback."""
    with app_client() as web:
        assert web.app.state.nasdaq_bars_provider("AAPL", timeframe)

    (request,) = alpaca_fake.instances[-1].requests
    assert request["timeframe"] == expected_tf
    assert (request["end"] - request["start"]) == timedelta(days=lookback_days)


def test_nasdaq_forecast_route_fails_closed_on_kronos_not_on_missing_bars(alpaca_fake, app_client):
    """Route-level: bars are acquired, then the real Kronos gate answers (never a mock)."""
    with app_client() as web:
        response = web.post(
            "/api/model-lab/kronos/forecast",
            headers=AUTH,
            json={"market": "NASDAQ", "symbol": "aapl", "timeframe": "1d", "dataset_id": None},
        )
        live = web.get("/api/markets/live/status", headers=AUTH).json()

    assert alpaca_fake.instances[-1].requests, "the route never reached the NASDAQ bar provider"
    detail = str(response.json().get("detail", ""))
    # The reported symptom: bars were "unavailable" because of a NameError.
    assert "name 'datetime' is not defined" not in detail
    assert "read-only bars unavailable" not in detail
    assert "datetime" not in detail
    # Fail-closed on the Kronos gate itself, with no fabricated forecast.
    assert response.status_code == 409
    assert "Kronos" in detail

    # The forecast never touches trading authorization.
    assert live["default_live_execution"] == "OFF"
    assert live["nse"]["availability"] == "NOT_AVAILABLE"
    assert live["nasdaq"]["availability"] == "NOT_AVAILABLE"


def test_nse_quote_provider_timestamps_local_receipt(monkeypatch, app_client):
    """Sibling call site of the same missing import: the NSE quote receipt timestamp."""
    quote = Quote(
        instrument_key="NSE_EQ|INE002A01018",
        bid=100.0,
        ask=100.5,
        last=100.25,
        volume=1_000.0,
        timestamp=datetime(2026, 11, 5, 9, 45, tzinfo=UTC),
    )

    class FakeReadOnly:
        def __init__(self, token_provider, **kwargs) -> None:
            self._token_provider = token_provider

        def quote_snapshot(self, instrument_key: str) -> Quote:
            return replace(quote, instrument_key=instrument_key)

    # The app imports UpstoxReadOnly inside the lifespan, so patch before startup.
    monkeypatch.setattr(upstox_module, "UpstoxReadOnly", FakeReadOnly)

    with app_client() as web:
        lookup = web.app.state.live_paper_engine.nse_quote_provider
        snapshot = lookup("NSE_EQ|INE002A01018")

    assert snapshot["instrument_key"] == "NSE_EQ|INE002A01018"
    assert snapshot["feed"] == "upstox_nse_v2"
    assert snapshot["is_delayed"] is False
    received = datetime.fromisoformat(snapshot["local_received_at"])
    assert received.tzinfo is not None and received.utcoffset() == timedelta(0)


def test_server_module_binds_the_clock_names_its_providers_use():
    """Cheap root-cause guard: the closures read datetime/timedelta from module globals."""
    assert server.datetime is datetime
    assert server.timedelta is timedelta
    assert server.UTC is UTC
