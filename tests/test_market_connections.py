"""Market-connection tests: authentication, symbols, entitlements, fail-closed.

All provider HTTP is mocked (httpx.MockTransport). These tests prove AXIOM's
behavior around real providers — they are NOT proof of real provider
connectivity, which requires the owner's credentials and network. No secret is
ever asserted into logs or outputs; fake canary credentials prove redaction.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config.settings import Settings
from app.data.alpaca import (
    AlpacaAssetLookup,
    AlpacaHistoricalProvider,
    feed_label,
    validate_us_symbol,
)
from app.data.factory import stream_provider
from app.data.realtime import ReplayStream
from app.market_data.connection_probe import (
    probe_alpaca_connection,
    probe_upstox_connection,
)
from app.markets.us_calendar import USEquitiesCalendar, early_close
from datetime import date, datetime, timezone

TOKEN_CANARY = "FAKE-upstox-token-canary-000"
KEY_CANARY = "FAKE-alpaca-key-canary"
SECRET_CANARY = "FAKE-alpaca-secret-canary"


def upstox_settings(**overrides) -> Settings:
    values = dict(
        market_data_access_token=TOKEN_CANARY,
        market_data_symbols="NSE_EQ|INE002A01018",
    )
    values.update(overrides)
    return Settings(**values)


def alpaca_settings(**overrides) -> Settings:
    values = dict(
        alpaca_api_key=KEY_CANARY,
        alpaca_api_secret=SECRET_CANARY,
        alpaca_data_feed="iex",
        alpaca_symbols="AAPL,MSFT",
    )
    values.update(overrides)
    return Settings(**values)


def run_async(coro):
    return asyncio.run(coro)


# --- Upstox authentication handling -----------------------------------------

def test_upstox_probe_feed_ok_and_no_secret_echo():
    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.headers["Authorization"] == f"Bearer {TOKEN_CANARY}"
        return httpx.Response(
            200, json={"data": {"authorized_redirect_uri": "wss://feed.upstox.test/v3"}}
        )

    result = run_async(
        probe_upstox_connection(upstox_settings(), transport=httpx.MockTransport(handler))
    )
    assert result["verdict"] == "feed_ok" and result["ok"] is True
    assert result["authenticated"] is True
    assert result["market"] == "india" and result["currency"] == "INR"
    assert result["execution_mode"] == "AXIOM_PAPER" and result["real_order_api_used"] is False
    assert TOKEN_CANARY not in json.dumps(result)
    assert seen_paths == ["/v3/feed/market-data-feed/authorize"]


def test_upstox_probe_401_gives_token_expiry_and_reconnect_guidance():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "token expired"})

    result = run_async(
        probe_upstox_connection(upstox_settings(), transport=httpx.MockTransport(handler))
    )
    assert result["verdict"] == "rejected" and result["ok"] is False
    assert result["authenticated"] is False
    guidance = result["guidance"].lower()
    assert "token" in guidance and ("authoriz" in guidance or "login" in guidance)
    assert TOKEN_CANARY not in json.dumps(result)


def test_upstox_probe_unreachable_is_honest_not_connected():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network blocked", request=request)

    result = run_async(
        probe_upstox_connection(upstox_settings(), transport=httpx.MockTransport(handler))
    )
    assert result["verdict"] == "unreachable" and result["ok"] is False
    assert "not a verified connection" in result["guidance"].lower() or (
        "connectivity" in result["guidance"].lower()
    )


def test_upstox_probe_missing_credential_fails_closed():
    result = run_async(
        probe_upstox_connection(
            upstox_settings(market_data_access_token=""),
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        )
    )
    assert result["verdict"] == "invalid_input" and result["ok"] is False
    assert result["credential_present"] is False


# --- Alpaca authentication, entitlements, rate limits ------------------------

def _alpaca_pair_handler(bar_status=200, asset_payload=None, asset_status=200):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url.host), request.url.path))
        if request.url.path.endswith("/bars/latest"):
            assert request.url.params.get("feed") in {"iex", "sip", "delayed_sip"}
            return httpx.Response(bar_status, json={"bar": {"t": "2026-10-28T14:30:00Z"}})
        payload = asset_payload or {
            "symbol": request.url.path.rsplit("/", 1)[-1],
            "class": "us_equity", "exchange": "NASDAQ", "status": "active",
            "tradable": True, "name": "Apple Inc",
        }
        return httpx.Response(asset_status, json=payload)

    return handler, seen


def test_alpaca_probe_feed_ok_labels_iex_never_consolidated():
    handler, seen = _alpaca_pair_handler()
    result = probe_alpaca_connection(
        alpaca_settings(), symbol="AAPL", transport=httpx.MockTransport(handler),
    )
    assert result["verdict"] == "feed_ok" and result["ok"] is True
    assert result["feed"] == "iex" and result["feed_is_consolidated"] is False
    label = result["feed_label"]
    assert "single-venue" in label and "NOT the consolidated" in label
    assert "all US exchanges" not in label
    assert result["listing"]["ok"] is True
    asset = result["listing"]["asset"]
    assert asset["listing_venue"] == "NASDAQ" and asset["currency"] == "USD"
    assert asset["instrument_class"] == "us_equity"
    assert KEY_CANARY not in json.dumps(result) and SECRET_CANARY not in json.dumps(result)
    # Read-only boundary: only the market-data host and the metadata host.
    assert {host for host, _ in seen} == {"data.alpaca.markets", "api.alpaca.markets"}


def test_alpaca_probe_sip_label_says_consolidated():
    handler, _ = _alpaca_pair_handler()
    result = probe_alpaca_connection(
        alpaca_settings(alpaca_data_feed="sip"), symbol="AAPL",
        transport=httpx.MockTransport(handler),
    )
    assert result["feed_is_consolidated"] is True
    assert "all US exchanges" in result["feed_label"]
    assert feed_label("sip") != feed_label("iex")
    assert feed_label("delayed_sip") != feed_label("sip")


def test_alpaca_probe_403_forbidden_explains_entitlement():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "not entitled"})

    result = probe_alpaca_connection(
        alpaca_settings(alpaca_data_feed="sip"), symbol="AAPL",
        transport=httpx.MockTransport(handler),
    )
    assert result["verdict"] == "forbidden" and result["ok"] is False
    assert "entitled" in result["guidance"].lower()
    assert "iex" in result["guidance"].lower() and "sip" in result["guidance"].lower()


def test_alpaca_probe_429_rate_limited_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "too many requests"})

    result = probe_alpaca_connection(
        alpaca_settings(), symbol="AAPL", transport=httpx.MockTransport(handler),
    )
    assert result["verdict"] == "rate_limited" and result["ok"] is False
    assert "429" in result["guidance"] and "never" in result["guidance"].lower()


def test_alpaca_probe_unreachable_never_claims_connected():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network blocked", request=request)

    result = probe_alpaca_connection(
        alpaca_settings(), symbol="AAPL", transport=httpx.MockTransport(handler),
    )
    assert result["verdict"] == "unreachable" and result["ok"] is False
    assert result["authenticated"] is False


def test_alpaca_probe_missing_credential_and_bad_symbol():
    result = probe_alpaca_connection(
        alpaca_settings(alpaca_api_key="", alpaca_api_secret=""),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert result["verdict"] == "invalid_input" and result["credential_present"] is False
    result = probe_alpaca_connection(
        alpaca_settings(), symbol="not a ticker",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert result["verdict"] == "invalid_input" and result["ok"] is False


# --- Symbol lookup / instrument metadata -------------------------------------

def test_alpaca_asset_lookup_accepts_us_equity_and_rejects_everything_else():
    def handler_for(payload, status=200):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload)
        return handler

    def lookup_with(payload, status=200):
        client = httpx.Client(transport=httpx.MockTransport(handler_for(payload, status)))
        return AlpacaAssetLookup(KEY_CANARY, SECRET_CANARY, client=client)

    # Non-equity class is refused with a specific message.
    with pytest.raises(ValueError, match="not a U.S.-listed equity/ETF"):
        lookup_with({
            "symbol": "AAPL", "class": "crypto", "exchange": "CRYPTO",
            "status": "active", "tradable": True,
        }).lookup("AAPL")

    # Inactive listings are refused.
    with pytest.raises(ValueError, match="not an active listing"):
        lookup_with({
            "symbol": "AAPL", "class": "us_equity", "exchange": "NASDAQ",
            "status": "inactive", "tradable": False,
        }).lookup("AAPL")

    # Unknown tickers are refused.
    with pytest.raises(ValueError, match="not a listing known to Alpaca"):
        lookup_with({"message": "not found"}, status=404).lookup("ZZZZ")

    # An active NASDAQ equity returns explicit venue/type metadata.
    asset = lookup_with({
        "symbol": "AAPL", "class": "us_equity", "exchange": "NASDAQ",
        "status": "active", "tradable": True, "name": "Apple Inc",
    }).lookup("AAPL")
    assert asset["listing_venue"] == "NASDAQ"
    assert asset["instrument_class"] == "us_equity"
    assert asset["currency"] == "USD" and asset["read_only"] is True

    with pytest.raises(ValueError, match="Invalid symbol|plain listed tickers"):
        validate_us_symbol("lower case")


def test_upstox_instrument_key_validation_is_explicit():
    # Invalid keys are refused at configuration time with a clear example.
    with pytest.raises(ValueError, match="instrument"):
        Settings(data_source="upstox", market_data_provider="upstox",
                 market_data_symbols="AAPL")
    ok = Settings(data_source="upstox", market_data_provider="upstox",
                  market_data_symbols="NSE_EQ|INE002A01018,BSE_EQ|INE002A01018")
    assert ok.market_data_symbol_list() == ["NSE_EQ|INE002A01018", "BSE_EQ|INE002A01018"]


# --- History/stream integration + fail-closed boundaries ---------------------

def test_alpaca_historical_rate_limit_fails_closed_with_clear_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "slow down"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AlpacaHistoricalProvider(KEY_CANARY, SECRET_CANARY, client=client)
    with pytest.raises(ValueError, match="rate limit"):
        provider.historical(
            "AAPL", "1m",
            start=datetime(2026, 1, 2, tzinfo=timezone.utc),
            end=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )


def test_failed_provider_connection_never_falls_back_to_demo_replay():
    # No credentials -> construction fails loudly; it never yields the
    # bundled DEMO ReplayStream or any sample-data substitute.
    us = Settings(market="us", currency="USD", market_calendar="us-equities",
                  default_timeframe="1m", alpaca_symbols="AAPL")
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        stream_provider(us, "alpaca", symbol="AAPL")
    india = Settings(market_data_symbols="NSE_EQ|INE002A01018",
                     market_data_access_token="")
    with pytest.raises(ValueError, match="access token"):
        stream_provider(india, "upstox")


def test_probes_never_reference_order_endpoints():
    handler, seen = _alpaca_pair_handler()
    probe_alpaca_connection(
        alpaca_settings(), symbol="AAPL", transport=httpx.MockTransport(handler),
    )
    for host, path in seen:
        assert host in {"data.alpaca.markets", "api.alpaca.markets"}
        assert "order" not in path.lower()


# --- Session calendars -------------------------------------------------------

def test_us_calendar_dst_early_close_and_holiday_sanity():
    calendar = USEquitiesCalendar()
    # Monday 2026-03-09 is the first session after spring-forward (EDT).
    stamp = datetime(2026, 3, 9, 14, 5, tzinfo=timezone.utc)  # 10:05 America/New_York
    assert calendar.is_open(stamp) is True
    assert calendar.market_state(stamp)["status"] == "OPEN"
    # Day after Thanksgiving 2026 is a 13:00 early close.
    assert early_close(date(2026, 11, 27)) is True
    bounds = calendar.session_bounds(date(2026, 11, 27))
    assert bounds[1].strftime("%H:%M") == "13:00"
    # July 4, 2026 falls on Saturday: Friday July 3 is the observed holiday
    # and Thursday July 2 is the early close.
    assert calendar.session_bounds(date(2026, 7, 3)) is None
    assert early_close(date(2026, 7, 2)) is True
    # A weekday night is CLOSED with a next transition.
    night = datetime(2026, 3, 10, 2, 0, tzinfo=timezone.utc)
    state = calendar.market_state(night)
    assert state["status"] == "CLOSED" and state["next_transition"]
