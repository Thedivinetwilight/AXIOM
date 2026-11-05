"""Regression: the NASDAQ Kronos forecast path must never fail with ``NameError``.

Path traced here (the one the operator UI drives):

    terminal.js  #ml-forecast-form   (Market = NASDAQ, "Run Validated Forecast")
      -> POST /api/model-lab/kronos/forecast        (app/api/routes/model_lab.py)
      -> app.state.nasdaq_bars_provider             (installed in app/api/server.py)
      -> _nasdaq_live_bars_lookup                   (closure inside create_app)
      -> AlpacaHistoricalProvider.historical()      (read-only market data)

Two defects lived on that path:

1. ``_nasdaq_live_bars_lookup`` computed ``datetime.now(timezone.utc)`` while
   ``app/api/server.py`` imported neither ``datetime`` nor ``timezone``, so every
   NASDAQ forecast raised ``NameError: name 'datetime' is not defined``.
2. The closure (and the ``/api/markets/chart`` route) called
   ``AlpacaHistoricalProvider.get_candles(...)``. The adapter's only public read
   API is ``MarketDataProvider.historical(symbol, timeframe, start, end)``, so the
   call raised ``AttributeError: 'AlpacaHistoricalProvider' object has no
   attribute 'get_candles'``.

Both surfaced through the same fail-closed branch as "read-only bars
unavailable", which looked like a credentials/feed problem instead of a bug.

The tests below drive the real closures. The first group uses a stand-in provider
recording the call; the second group (``test_..._through_real_provider_wrapper``)
keeps the *real* ``AlpacaHistoricalProvider`` and mocks only its HTTP transport,
so a wrong method name can never again hide behind a hand-written fake.
No network, no credentials, no broker session, no orders, and Kronos model
weights are never required. Live execution stays OFF.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
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

    def historical(self, symbol: str, timeframe: str, start: datetime, end: datetime):
        # Mirrors the real adapter's public API (MarketDataProvider.historical).
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


# ---------------------------------------------------------------------------
# Second group: the REAL provider wrapper with a mocked HTTP transport.
#
# The stand-in above hides the adapter's surface, which is exactly how
# ``prov.get_candles(...)`` survived a green suite while every NASDAQ forecast
# failed at runtime. These tests keep ``AlpacaHistoricalProvider`` real and mock
# only ``httpx.Client``, so any call to a method the adapter does not expose
# raises AttributeError here too.
# ---------------------------------------------------------------------------

_API_TF_SECONDS = {"1Min": 60, "5Min": 300, "15Min": 900, "30Min": 1800,
                   "1Hour": 3600, "1Day": 86400}


def _alpaca_bar_rows(*, symbol: str, timeframe: str, start: datetime,
                     end: datetime) -> list[dict]:
    """Canned Alpaca ``/v2/stocks/bars`` rows inside the requested [start, end)."""
    step = timedelta(seconds=_API_TF_SECONDS[timeframe])
    stamps: list[datetime] = []
    cursor = end - step
    while cursor >= start and len(stamps) < 500:
        stamps.append(cursor)
        cursor -= step
    rows: list[dict] = []
    px = 100.0
    for stamp in reversed(stamps):
        nxt = px * 1.002
        rows.append(
            {
                "t": stamp.isoformat().replace("+00:00", "Z"),
                "o": round(px, 4),
                "h": round(max(px, nxt) * 1.001, 4),
                "l": round(min(px, nxt) * 0.999, 4),
                "c": round(nxt, 4),
                "v": 25_000,
            }
        )
        px = nxt
    return rows


class _FakeHTTPResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=httpx.Request("GET", "https://data.alpaca.markets"),
                response=httpx.Response(self.status_code),
            )


class FakeAlpacaHTTPClient:
    """Records every read-only request; answers bars only, offline for the rest."""

    instances: list["FakeAlpacaHTTPClient"] = []
    offline = False

    def __init__(self, *args, **kwargs) -> None:
        self.headers = dict(kwargs.get("headers") or {})
        self.requests: list[dict] = []
        self.closed = False
        FakeAlpacaHTTPClient.instances.append(self)

    def get(self, url: str, params=None) -> _FakeHTTPResponse:
        params = {k: str(v) for k, v in dict(params or {}).items()}
        self.requests.append({"url": url, "params": params})
        if not FakeAlpacaHTTPClient.offline and "/v2/stocks/bars" in url:
            start = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
            symbol = params["symbols"]
            return _FakeHTTPResponse(
                {
                    "bars": {
                        symbol: _alpaca_bar_rows(
                            symbol=symbol, timeframe=params["timeframe"], start=start, end=end
                        )
                    },
                    "next_page_token": None,
                }
            )
        # Quotes/assets are not under test: fail closed like a disconnected host.
        raise httpx.ConnectError("offline: the regression suite never dials Alpaca")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def real_alpaca_wrapper(monkeypatch):
    """Keep ``AlpacaHistoricalProvider`` real; mock only its HTTP transport."""
    FakeAlpacaHTTPClient.instances = []
    FakeAlpacaHTTPClient.offline = False
    monkeypatch.setattr(alpaca_module.httpx, "Client", FakeAlpacaHTTPClient)
    return FakeAlpacaHTTPClient


def _bars_requests(client: FakeAlpacaHTTPClient) -> list[dict]:
    return [r for r in client.requests if "/v2/stocks/bars" in r["url"]]


def test_recording_stand_in_mirrors_the_adapter_public_read_api():
    """The fake used above must expose exactly the API the adapter really has."""
    from app.data.alpaca import AlpacaHistoricalProvider

    assert hasattr(AlpacaHistoricalProvider, "historical")
    assert not hasattr(AlpacaHistoricalProvider, "get_candles"), (
        "the adapter never had get_candles(); fix the caller, not this test"
    )
    assert hasattr(RecordingAlpacaProvider, "historical")
    assert not hasattr(RecordingAlpacaProvider, "get_candles")


@pytest.fixture
def forecast_client(app_client):
    """App wired like the operator UI: NASDAQ credentials present, nothing dialled."""

    def _build():
        return app_client()

    return _build


def test_forecast_bar_lookup_drives_the_real_provider_wrapper(real_alpaca_wrapper,
                                                              forecast_client):
    """``nasdaq_bars_provider`` must reach the adapter and return validated bars."""
    with forecast_client() as web:
        bars = web.app.state.nasdaq_bars_provider("aapl", "1d")

    assert bars, "the NASDAQ forecast bar lookup returned no bars"
    assert {bar.symbol for bar in bars} == {"AAPL"}, "symbol must be normalised first"
    assert all(bar.timestamp.utcoffset() == timedelta(0) for bar in bars)
    assert all(a.timestamp < b.timestamp for a, b in zip(bars, bars[1:])), "bars must ascend"

    client = real_alpaca_wrapper.instances[0]
    assert client.closed is True, "the adapter must always be closed"

    (request,) = _bars_requests(client)
    assert request["url"].endswith("/v2/stocks/bars")
    assert request["params"]["symbols"] == "AAPL"
    assert request["params"]["timeframe"] == "1Day", "Kronos 1d context reads Alpaca 1D bars"
    assert request["params"]["feed"] == "iex"
    assert request["params"]["adjustment"] == "raw"

    start = datetime.fromisoformat(request["params"]["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(request["params"]["end"].replace("Z", "+00:00"))
    assert start.utcoffset() == timedelta(0) and end.utcoffset() == timedelta(0)
    assert end - start == timedelta(days=240), "1D forecast context keeps the 240-day window"
    assert all(start <= bar.timestamp < end for bar in bars), "bars outside [start, end) leaked"


@pytest.mark.parametrize(
    ("timeframe", "expected_tf", "lookback_days"),
    [("1d", "1Day", 240), ("4h", "1Hour", 30), ("5m", "5Min", 7)],
)
def test_nasdaq_forecast_timeframe_mapping_through_real_wrapper(
    real_alpaca_wrapper, forecast_client, timeframe, expected_tf, lookback_days
):
    """Alpaca has no 4h bars, so 4h context is fetched as 1h with the 30-day window."""
    with forecast_client() as web:
        assert web.app.state.nasdaq_bars_provider("AAPL", timeframe)

    (request,) = _bars_requests(real_alpaca_wrapper.instances[0])
    assert request["params"]["timeframe"] == expected_tf
    start = datetime.fromisoformat(request["params"]["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(request["params"]["end"].replace("Z", "+00:00"))
    assert end - start == timedelta(days=lookback_days)


def test_nasdaq_forecast_route_reads_bars_through_the_real_wrapper(real_alpaca_wrapper,
                                                                   forecast_client):
    """POST /api/model-lab/kronos/forecast must not report a missing adapter method."""
    with forecast_client() as web:
        response = web.post(
            "/api/model-lab/kronos/forecast",
            headers=AUTH,
            json={"market": "NASDAQ", "symbol": "aapl", "timeframe": "1d", "dataset_id": None},
        )

    detail = str(response.json().get("detail", ""))
    assert "get_candles" not in detail
    assert "has no attribute" not in detail
    assert "read-only bars unavailable" not in detail, (
        "bars were still reported unavailable: the forecast path never reached Alpaca"
    )
    # Bars were really fetched through the wrapper, then the honest Kronos gate answered.
    assert _bars_requests(real_alpaca_wrapper.instances[0]), "the wrapper was never called"
    assert response.status_code == 409
    assert "Kronos" in detail


def test_nasdaq_forecast_fails_closed_when_bars_are_unavailable(real_alpaca_wrapper,
                                                                forecast_client):
    """No bars means an explicit 409 — never sample/simulated bars, never a forecast."""
    real_alpaca_wrapper.offline = True
    with forecast_client() as web:
        response = web.post(
            "/api/model-lab/kronos/forecast",
            headers=AUTH,
            json={"market": "NASDAQ", "symbol": "AAPL", "timeframe": "1d", "dataset_id": None},
        )

    body = response.json()
    detail = str(body.get("detail", ""))
    assert response.status_code == 409
    assert "read-only bars unavailable" in detail
    assert "Mock fallback is disabled" in detail
    assert "bars" not in body, "no fabricated bars may be returned on failure"


def test_nasdaq_chart_route_reads_bars_through_the_real_wrapper(real_alpaca_wrapper,
                                                                forecast_client):
    """Sibling read-only call site: /api/markets/chart NASDAQ uses the same API."""
    with forecast_client() as web:
        response = web.get(
            "/api/markets/chart",
            headers=AUTH,
            params={"market": "NASDAQ", "symbol": "AAPL", "timeframe": "1D"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["bars"], "the chart returned no read-only NASDAQ bars"
    assert payload["simulated_bars"] is False
    assert payload["real_order_api_used"] is False
    error = str(payload.get("error") or "")
    assert "get_candles" not in error
    assert "has no attribute" not in error
    (request,) = _bars_requests(real_alpaca_wrapper.instances[0])
    assert request["params"]["timeframe"] == "1Day"
