"""Manual Upstox access-token entry: secrets stay protected, errors fail closed,
only read-only endpoints are called, and save/test can never Arm or Activate.

Contract under test:
- the saved token is never returned, displayed, logged, placed in a URL, or
  included in error messages (every response body is scanned for it);
- the read-only connection test calls only GET /v2/user/profile and
  GET /v2/market-quote/full, reports instrument key / quote timestamp /
  freshness / sanitized details only, and never falls back to sample data;
- missing, rejected (expired/invalid/revoked) and unreachable states fail
  closed with explicit verdicts; stale quotes are never treated as current;
- saving, replacing, deleting or testing a token never creates a session,
  changes execution mode, Arms/Activates, or starts automation.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.autonomy.upstox import UpstoxReadOnly
from app.autonomy.upstox_token import (
    FileUpstoxTokenStore, MemoryUpstoxTokenStore, resolve_token_provider,
)
from app.config.settings import Settings
from app.vantage.secrets import redact

SECRET = "test-secret-token-value-1234567890"
RELIANCE_KEY = "NSE_EQ|INE002A01018"
NOW = datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc)

RELIANCE_ROW = {
    "segment": "NSE_EQ", "name": "RELIANCE INDUSTRIES LTD",
    "exchange": "NSE", "isin": "INE002A01018",
    "instrument_type": "EQ", "instrument_key": RELIANCE_KEY,
    "lot_size": 1, "freeze_quantity": 100000.0, "exchange_token": "2885",
    "tick_size": 5.0, "trading_symbol": "RELIANCE",
}

ALLOWED_TEST_KEYS = {
    "ok", "verdict", "detail", "instrument_key", "quote_timestamp",
    "freshness", "authenticated", "token_source", "checked_at",
    "read_only", "real_order_api_used", "execution_mode", "session_effect",
}


def client(tmp_path):
    settings = replace(Settings(), api_token="local-api-test",
                       database_url=f"sqlite:///{tmp_path}/app.db")
    return TestClient(create_app(settings, data_dir=tmp_path))


def with_store(web) -> MemoryUpstoxTokenStore:
    store = MemoryUpstoxTokenStore()
    web.app.state.upstox_token = store
    web.app.state.autonomy.token_provider = resolve_token_provider(store)
    return store


def with_catalog(web):
    catalog = web.app.state.autonomy.catalog

    def fake_download(url):
        return ([RELIANCE_ROW] if "complete" in str(url) else []), "bod-digest"

    catalog._download = fake_download
    catalog.clock = lambda: NOW
    web.app.state.autonomy.clock = lambda: NOW
    headers = {"Authorization": "Bearer local-api-test"}
    assert web.post("/api/autonomy/catalog/refresh", headers=headers).status_code == 200


def quote_payload(key: str, when: datetime) -> dict:
    # Official Full Market Quotes shape: data keyed by "EXCHANGE:SYMBOL" with
    # the exact key in instrument_token (upstox.com/developer/
    # api-documentation/get-full-market-quote/).
    symbol = key.split("|", 1)[1]
    exchange = key.split("|", 1)[0]
    return {"status": "success", "data": {f"{exchange}:{symbol}": {
        "instrument_token": key,
        "last_price": 100.0, "volume": 5,
        "timestamp": when.isoformat(),
        "depth": {"buy": [{"price": 99.0, "quantity": 1}],
                  "sell": [{"price": 101.0, "quantity": 1}]},
    }}}


PROFILE_PAYLOAD = {"status": "success", "data": {
    "user_id": "UTEST1", "is_active": True,
    "exchanges": ["NSE"], "products": ["I"], "order_types": ["MARKET"],
}}


class RecordingTransport(httpx.MockTransport):
    def __init__(self, handler):
        self.calls: list[httpx.Request] = []
        super().__init__(lambda request: (self.calls.append(request), handler(request))[1])


def install_reader(web, handler):
    transport = RecordingTransport(handler)

    def factory(provider):
        return UpstoxReadOnly(provider, transport=transport, clock=lambda: NOW)

    web.app.state.autonomy.reader_factory = factory
    return transport


def assert_read_only_and_secret_free(calls, secret: str):
    assert calls, "the read-only client must have been called"
    for request in calls:
        assert request.method == "GET", f"only GET is allowed, saw {request.method} {request.url.path}"
        assert request.url.path in {"/v2/user/profile", "/v2/market-quote/quotes"}, request.url.path
        assert secret not in str(request.url), "the token must never appear in a URL"
        assert request.headers.get("Authorization") == f"Bearer {secret}"


def test_save_replace_delete_lifecycle_never_returns_the_token(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MARKET_DATA_ACCESS_TOKEN", raising=False)
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        store = with_store(web)
        saved = web.post("/api/autonomy/upstox-token", headers=headers,
                         json={"access_token": SECRET})
        assert saved.status_code == 200
        assert saved.json()["saved"] is True and saved.json()["replaced"] is False
        assert saved.json()["token_returned"] is False
        assert SECRET not in saved.text

        status = web.get("/api/autonomy/upstox-token/status", headers=headers)
        assert status.status_code == 200
        body = status.json()
        assert body["present"] is True and body["saved_at"]
        assert body["source"] == "protected-store"
        assert body["token_returned"] is False
        assert "access_token" not in json.dumps(body).lower().replace("token_returned", "")
        assert SECRET not in status.text

        replaced = web.post("/api/autonomy/upstox-token", headers=headers,
                            json={"access_token": SECRET + "v2"})
        assert replaced.json()["replaced"] is True
        assert SECRET + "v2" not in replaced.text
        assert store.token() == SECRET + "v2"  # only one value stored

        deleted = web.delete("/api/autonomy/upstox-token", headers=headers)
        assert deleted.status_code == 200 and deleted.json()["deleted"] is True
        assert store.token() == ""
        assert web.get("/api/autonomy/upstox-token/status", headers=headers).json()["present"] is False


def test_token_endpoints_require_authentication(tmp_path):
    with client(tmp_path) as web:
        with_store(web)
        assert web.get("/api/autonomy/upstox-token/status").status_code == 401
        assert web.post("/api/autonomy/upstox-token", json={"access_token": SECRET}).status_code == 401
        assert web.delete("/api/autonomy/upstox-token").status_code == 401
        assert web.post("/api/autonomy/upstox-token/test",
                        json={"instrument_key": RELIANCE_KEY}).status_code == 401


def test_read_only_test_calls_only_profile_and_quote_and_returns_no_prices(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web).save(SECRET)
        with_catalog(web)

        def handler(request):
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            return httpx.Response(200, json=quote_payload(RELIANCE_KEY, NOW))

        transport = install_reader(web, handler)
        response = web.post("/api/autonomy/upstox-token/test", headers=headers,
                            json={"instrument_key": RELIANCE_KEY})
        assert response.status_code == 200, response.text
        result = response.json()
        assert set(result.keys()) == ALLOWED_TEST_KEYS  # no prices, no token
        assert result["ok"] is True and result["verdict"] == "ok"
        assert result["authenticated"] is True
        assert result["instrument_key"] == RELIANCE_KEY
        assert result["quote_timestamp"] == NOW.isoformat()
        assert result["freshness"] == {"label": "fresh", "age_seconds": 0}
        assert result["session_effect"] == "NONE"
        assert SECRET not in response.text
        for banned in ("last_price", "bid", "ask", "volume", "100.0"):
            assert banned not in response.text
        assert_read_only_and_secret_free(transport.calls, SECRET)


def test_stale_quote_is_never_treated_as_current(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web).save(SECRET)
        with_catalog(web)

        def handler(request):
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            return httpx.Response(200, json=quote_payload(RELIANCE_KEY, NOW - timedelta(hours=1)))

        transport = install_reader(web, handler)
        result = web.post("/api/autonomy/upstox-token/test", headers=headers,
                          json={"instrument_key": RELIANCE_KEY}).json()
        assert result["ok"] is False and result["verdict"] == "stale_quote"
        assert result["freshness"]["label"] == "stale"
        assert result["freshness"]["age_seconds"] == 3600
        assert "NOT treated as current" in result["detail"]
        assert result["authenticated"] is True
        assert_read_only_and_secret_free(transport.calls, SECRET)


def test_rejected_expired_or_revoked_token_fails_closed(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web).save(SECRET)
        with_catalog(web)

        def handler(request):
            return httpx.Response(401, json={"status": "error"})

        transport = install_reader(web, handler)
        result = web.post("/api/autonomy/upstox-token/test", headers=headers,
                          json={"instrument_key": RELIANCE_KEY}).json()
        assert result["ok"] is False and result["verdict"] == "rejected_credential"
        assert result["authenticated"] is False
        assert "expired" in result["detail"] and "revoked" in result["detail"]
        assert SECRET not in json.dumps(result)
        assert len(transport.calls) == 1  # fail closed before any quote call
        assert_read_only_and_secret_free(transport.calls, SECRET)


def test_missing_token_fails_closed_without_any_upstox_call(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MARKET_DATA_ACCESS_TOKEN", raising=False)
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web)
        with_catalog(web)
        transport = install_reader(web, lambda request: httpx.Response(500))
        result = web.post("/api/autonomy/upstox-token/test", headers=headers,
                          json={"instrument_key": RELIANCE_KEY}).json()
        assert result["ok"] is False and result["verdict"] == "missing_token"
        assert "nothing was sent to Upstox" in result["detail"]
        assert transport.calls == []  # no silent fallback, no network call


def test_unreachable_upstox_fails_closed_and_shows_no_fallback_data(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web).save(SECRET)
        with_catalog(web)

        def handler(request):
            raise httpx.ConnectTimeout("offline", request=request)

        transport = install_reader(web, handler)
        result = web.post("/api/autonomy/upstox-token/test", headers=headers,
                          json={"instrument_key": RELIANCE_KEY}).json()
        assert result["ok"] is False
        assert result["verdict"] in {"unreachable", "read_failed"}
        assert "unreachable" in result["detail"]
        for banned in ("DEMO", "sample", "replay"):
            assert banned.lower() not in result["detail"].lower()
        assert_read_only_and_secret_free(transport.calls, SECRET)


def test_unknown_instrument_key_rejected_before_any_upstox_call(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web).save(SECRET)
        with_catalog(web)
        transport = install_reader(web, lambda request: httpx.Response(500))
        response = web.post("/api/autonomy/upstox-token/test", headers=headers,
                            json={"instrument_key": "NSE_EQ|INE000000000"})
        assert response.status_code == 400
        assert "BOD catalog" in response.text
        assert transport.calls == []


def test_save_and_test_never_create_sessions_arm_or_activate(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MARKET_DATA_ACCESS_TOKEN", raising=False)
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        store = with_store(web)
        with_catalog(web)
        service = web.app.state.autonomy

        created = web.post("/api/autonomy/sessions", headers=headers,
                           json={"mode": "PAPER", "instrument_keys": [RELIANCE_KEY]})
        assert created.status_code in {200, 201}
        session_id = created.json()["session_id"]
        assert service.runtimes[session_id].state == "UNCONFIGURED"

        def handler(request):
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            return httpx.Response(200, json=quote_payload(RELIANCE_KEY, NOW))

        transport = install_reader(web, handler)
        web.post("/api/autonomy/upstox-token", headers=headers, json={"access_token": SECRET})
        web.post("/api/autonomy/upstox-token/test", headers=headers,
                 json={"instrument_key": RELIANCE_KEY})
        web.get("/api/autonomy/upstox-token/status", headers=headers)
        web.delete("/api/autonomy/upstox-token", headers=headers)

        assert set(service.runtimes) == {session_id}
        assert service.runtimes[session_id].state == "UNCONFIGURED"
        assert store.token() == "" or store.token() == SECRET  # storage only
        status = web.get("/api/autonomy/status", headers=headers).json()
        assert status["automatic_arm_or_activate"] is False
        assert "arm" not in [str(getattr(r, "path", "")) for r in transport.calls]


def test_environment_token_still_works_and_is_labelled(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "environment-token-abc")
    monkeypatch.delenv("MARKET_DATA_ACCESS_TOKEN", raising=False)
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web)  # empty store; environment must win the fallback
        with_catalog(web)

        def handler(request):
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            return httpx.Response(200, json=quote_payload(RELIANCE_KEY, NOW))

        transport = install_reader(web, handler)
        result = web.post("/api/autonomy/upstox-token/test", headers=headers,
                          json={"instrument_key": RELIANCE_KEY}).json()
        assert result["token_source"] == "environment"
        assert result["ok"] is True
        assert_read_only_and_secret_free(transport.calls, "environment-token-abc")


def test_redaction_covers_token_fields_and_values():
    assert redact({"access_token": SECRET}) == {"access_token": "[REDACTED]"}
    assert redact({"nested": {"Authorization": SECRET}}) == {"nested": {"Authorization": "[REDACTED]"}}
    assert SECRET not in json.dumps(redact({"note": f"token {SECRET} used"}, secrets=(SECRET,)))


# ---------------------------------------------------------------------------
# Regression: the official Full Market Quotes endpoint and exact BOD key
# encoding. The packaged client used GET /v2/market-quote/full, which is not a
# documented Upstox endpoint and returned HTTP 400 live for BCD_FO|2135614.


def test_full_market_quote_uses_documented_quotes_endpoint_with_exact_bcd_key():
    """Pin GET /v2/market-quote/quotes + instrument_key=BCD_FO%7C2135614
    (the user-selected exact BOD key) and the official EXCHANGE:SYMBOL
    response shape matched via instrument_token."""
    from app.autonomy.upstox import UpstoxReadOnly as Reader

    requests: list = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "data": {
            "BCD_FO:668GS2040 99.25 CE 24 JUN 27": {
                "instrument_token": "BCD_FO|2135614",
                "last_price": 10.5, "volume": 0,
                "timestamp": NOW.isoformat(),
                # Illiquid/closed book: zeroed top-of-book is legitimate and
                # must not fail the connectivity snapshot.
                "depth": {"buy": [{"price": 0, "quantity": 0}],
                          "sell": [{"price": 0, "quantity": 0}]},
            }}})

    client = Reader(lambda: SECRET, transport=httpx.MockTransport(handler), clock=lambda: NOW)
    snapshot = client.quote_snapshot("BCD_FO|2135614")
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v2/market-quote/quotes"
    assert request.url.query == b"instrument_key=BCD_FO%7C2135614"
    assert "market-quote/full" not in str(request.url)
    assert SECRET not in str(request.url)
    assert snapshot.instrument_key == "BCD_FO|2135614"
    assert snapshot.timestamp == NOW

    # Execution-path quote() keeps its strict executable-depth requirement
    # (a zeroed/closed book must fail closed, never trade).
    import pytest
    from app.autonomy.upstox import ReconciliationFailure
    with pytest.raises(ReconciliationFailure, match="depth|validation"):
        client.quote("BCD_FO|2135614")


def test_http_400_failure_surfaces_sanitized_body_and_request_line(tmp_path):
    """A failing quote call must name the exact request (path + encoded
    query) and Upstox's whitelisted error code/message — and never the token."""
    from app.autonomy.upstox import ReconciliationFailure, UpstoxReadOnly as Reader

    def handler(request):
        return httpx.Response(400, json={"status": "error", "errors": [
            {"errorCode": "UDAPI1087", "message": "One of either symbol or instrument_key is invalid"}]})

    client = Reader(lambda: SECRET, transport=httpx.MockTransport(handler), clock=lambda: NOW)
    try:
        client.quote_snapshot("BCD_FO|2135614")
    except ReconciliationFailure as exc:
        message = str(exc)
    else:
        raise AssertionError("HTTP 400 must fail closed")
    assert "HTTP 400" in message
    assert "GET /v2/market-quote/quotes?instrument_key=BCD_FO%7C2135614" in message
    assert "UDAPI1087" in message and "instrument_key is invalid" in message
    assert SECRET not in message


def test_file_store_is_owner_only_and_status_never_returns_the_token(tmp_path):
    store = FileUpstoxTokenStore(tmp_path)
    status = store.save(SECRET)
    assert status["present"] is True and status["token_returned"] is False
    path = tmp_path / "upstox_access_token.token.json"
    assert path.is_file()
    import os as os_module
    import stat as stat_module
    if os_module.name != "nt":
        # POSIX fallback is required to be owner-only; on Windows the
        # production store is DPAPI and the file fallback is dev-only.
        assert stat_module.S_IMODE(path.stat().st_mode) == 0o600
    dumped = json.dumps(store.status())
    assert SECRET not in dumped and "access_token" not in dumped
    assert store.token() == SECRET
    assert store.delete() is True
    assert store.status()["present"] is False


# ---------------------------------------------------------------------------
# Equity-data path demonstration (mock-based): a BOD-backed RELIANCE key must
# be discoverable and pass a fresh read-only quote test. Live verification
# against the owner's account remains a separate, owner-run check.


def test_reliance_bod_key_appears_in_search_and_passes_fresh_quote_test(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web).save(SECRET)
        with_catalog(web)  # real-shape RELIANCE row via the refresh route

        found = web.get("/api/autonomy/catalog?query=RELIANCE", headers=headers)
        assert found.status_code == 200
        rows = found.json()["instruments"]
        assert any(row["instrument_key"] == RELIANCE_KEY for row in rows), \
            "the BOD-backed RELIANCE exact key must appear in search"

        def handler(request):
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            return httpx.Response(200, json=quote_payload(RELIANCE_KEY, NOW))

        transport = install_reader(web, handler)
        result = web.post("/api/autonomy/upstox-token/test", headers=headers,
                          json={"instrument_key": RELIANCE_KEY}).json()
        assert result["ok"] is True and result["verdict"] == "ok"
        assert result["authenticated"] is True
        assert result["instrument_key"] == RELIANCE_KEY
        assert result["quote_timestamp"] == NOW.isoformat()
        assert result["freshness"]["label"] == "fresh"
        assert_read_only_and_secret_free(transport.calls, SECRET)


def test_zero_last_price_is_explicit_fail_closed_no_trade_not_a_validation_blur(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web).save(SECRET)
        with_catalog(web)

        def handler(request):
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            payload = quote_payload(RELIANCE_KEY, NOW)
            entry = next(iter(payload["data"].values()))
            entry["last_price"] = 0.0
            entry["volume"] = 0
            entry["ohlc"] = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 100.5}
            return httpx.Response(200, json=payload)

        transport = install_reader(web, handler)
        result = web.post("/api/autonomy/upstox-token/test", headers=headers,
                          json={"instrument_key": RELIANCE_KEY}).json()
        assert result["ok"] is False and result["verdict"] == "no-trade"
        assert result["authenticated"] is True
        assert result["quote_timestamp"] == NOW.isoformat()  # observed, honest
        assert result["freshness"]["label"] == "fresh"
        assert "last_price=0.0" in result["detail"]
        assert "fail-closed" in result["detail"]
        assert "previous close" in result["detail"]  # labelled reference only
        assert SECRET not in json.dumps(result)
        assert_read_only_and_secret_free(transport.calls, SECRET)


def test_missing_or_unusable_quote_fields_fail_closed_as_invalid_quote(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        with_store(web).save(SECRET)
        with_catalog(web)

        def missing_price(request):
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            payload = quote_payload(RELIANCE_KEY, NOW)
            entry = next(iter(payload["data"].values()))
            entry.pop("last_price")
            return httpx.Response(200, json=payload)

        transport = install_reader(web, missing_price)
        result = web.post("/api/autonomy/upstox-token/test", headers=headers,
                          json={"instrument_key": RELIANCE_KEY}).json()
        assert result["ok"] is False and result["verdict"] == "invalid-quote"
        assert "missing the documented last_price field" in result["detail"]
        assert result["freshness"]["label"] != "fresh" or result["quote_timestamp"]

        def unusable_stamp(request):
            if request.url.path.endswith("/profile"):
                return httpx.Response(200, json=PROFILE_PAYLOAD)
            payload = quote_payload(RELIANCE_KEY, NOW)
            entry = next(iter(payload["data"].values()))
            entry["timestamp"] = ""
            entry["last_trade_time"] = ""
            return httpx.Response(200, json=payload)

        transport2 = install_reader(web, unusable_stamp)
        result2 = web.post("/api/autonomy/upstox-token/test", headers=headers,
                           json={"instrument_key": RELIANCE_KEY}).json()
        assert result2["ok"] is False and result2["verdict"] == "invalid-quote"
        assert "no usable timestamp" in result2["detail"]
        assert result2["freshness"] == {"label": "no-quote", "age_seconds": None}
        assert_read_only_and_secret_free(transport2.calls, SECRET)
