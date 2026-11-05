"""Phase 4 tests: Provider verification, 6 connection states, JWT expiry, and matrix."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from app.api.server import create_app
from app.config.settings import Settings
from app.market_data.provider_verification import (
    PROVIDER_CONNECTION_STATES,
    inspect_jwt_expiry,
    verify_alpaca_market_data,
    verify_upstox_market_data,
)


def _fake_jwt(exp_epoch: int, scopes: list[str] | None = None) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload_dict: dict = {"sub": "UP1234", "exp": exp_epoch}
    if scopes is not None:
        payload_dict["scope"] = scopes
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature_placeholder_not_logged"


def test_jwt_expiry_inspection_detects_valid_and_expired_without_leaking_token():
    now = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)
    future_tok = _fake_jwt(int(now.timestamp()) + 7200, scopes=["read_market_data"])
    info_ok = inspect_jwt_expiry(future_tok, now=now)
    assert info_ok["is_jwt"] is True and info_ok["expired"] is False
    assert info_ok["seconds_remaining"] == 7200
    assert info_ok["scopes"] == ["read_market_data"]
    assert future_tok not in json.dumps(info_ok)

    expired_tok = _fake_jwt(int(now.timestamp()) - 90)
    info_exp = inspect_jwt_expiry(expired_tok, now=now)
    assert info_exp["is_jwt"] is True and info_exp["expired"] is True


def test_upstox_verification_covers_disconnected_expired_429_fresh_and_offhours():
    assert set(PROVIDER_CONNECTION_STATES) == {
        "DISCONNECTED",
        "AUTHENTICATING",
        "CONNECTED_READ_ONLY",
        "STALE",
        "REJECTED",
        "UNAVAILABLE",
    }
    now_open = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)  # 11:30 IST open

    # 1. Disconnected
    d0 = verify_upstox_market_data("", now=now_open)
    assert d0["connection_state"] == "DISCONNECTED"
    assert d0["real_order_api_used"] is False

    # 2. Expired JWT rejected locally before network call
    exp_tok = _fake_jwt(int(now_open.timestamp()) - 10)
    d_exp = verify_upstox_market_data(exp_tok, now=now_open)
    assert d_exp["connection_state"] == "REJECTED"
    assert "expired" in d_exp["guidance"].lower()

    # 3. Rate limit HTTP 429 -> UNAVAILABLE
    valid_tok = _fake_jwt(int(now_open.timestamp()) + 3600)
    rl_transport = httpx.MockTransport(
        lambda _req: httpx.Response(429, headers={"Retry-After": "12"}, json={"status": "error"})
    )
    d_429 = verify_upstox_market_data(valid_tok, now=now_open, transport=rl_transport)
    assert d_429["connection_state"] == "UNAVAILABLE"
    assert d_429["verdict"] == "rate_limited"

    # 4. Connected read-only during NSE live session -> CONNECTED_READ_ONLY + quote
    def ok_handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        if req.url.path == "/v2/user/profile":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "user_id": "UP-DEMO-USER",
                        "is_active": True,
                        "exchanges": ["NSE", "BSE"],
                        "products": ["D", "I"],
                        "order_types": ["MARKET", "LIMIT", "SL"],
                    },
                },
            )
        if req.url.path == "/v2/market-quote/quotes":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "NSE_EQ:RELIANCE": {
                            "instrument_token": "NSE_EQ|INE002A01018",
                            "last_price": 1425.50,
                            "volume": 120000,
                            "last_trade_time": str(int(now_open.timestamp() * 1000)),
                            "depth": {
                                "buy": [{"price": 1425.40}],
                                "sell": [{"price": 1425.60}],
                            },
                        }
                    },
                },
            )
        raise AssertionError(f"unexpected path {req.url.path}")

    d_ok = verify_upstox_market_data(
        valid_tok,
        now=now_open,
        transport=httpx.MockTransport(ok_handler),
    )
    assert d_ok["connection_state"] == "CONNECTED_READ_ONLY"
    assert d_ok["quote"]["last"] == pytest.approx(1425.50)
    assert d_ok["quote"]["freshness"] == "FRESH"
    assert valid_tok not in json.dumps(d_ok)

    # 5. Off-hours weekend -> STALE (honest STALE_MARKET_CLOSED, never fake fresh)
    now_sun = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    sun_tok = _fake_jwt(int(now_sun.timestamp()) + 3600)
    d_sun = verify_upstox_market_data(sun_tok, now=now_sun, transport=httpx.MockTransport(ok_handler))
    assert d_sun["connection_state"] == "STALE"
    assert d_sun["quote"]["freshness"] == "STALE_MARKET_CLOSED"


def test_alpaca_verification_covers_entitlement_403_connected_and_dpapi_routes(tmp_path: Path):
    now_open = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)  # 10:30 EDT

    # SIP 403 -> REJECTED with actionable switch-to-IEX guidance
    sip_403 = httpx.MockTransport(lambda _r: httpx.Response(403, json={"message": "subscription does not permit SIP"}))
    res_403 = verify_alpaca_market_data("AK1", "AS1", feed="sip", now=now_open, transport=sip_403)
    assert res_403["connection_state"] == "REJECTED"
    assert "iex" in res_403["guidance"].lower()

    # Connected IEX snapshot during regular session -> CONNECTED_READ_ONLY
    def iex_ok(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.url.host == "data.alpaca.markets"
        return httpx.Response(
            200,
            json={
                "latestTrade": {"p": 230.25, "t": "2026-10-29T14:29:55Z"},
                "latestQuote": {"bp": 230.20, "ap": 230.30, "t": "2026-10-29T14:29:56Z"},
                "dailyBar": {"v": 980100},
            },
        )

    res_ok = verify_alpaca_market_data("AK-CANARY", "AS-CANARY", feed="iex", now=now_open, transport=httpx.MockTransport(iex_ok))
    assert res_ok["connection_state"] == "CONNECTED_READ_ONLY"
    assert res_ok["quote"]["last"] == pytest.approx(230.25)
    assert "CANARY" not in json.dumps(res_ok)

    # Full HTTP route test for /api/connections/matrix & /api/connections/alpaca/credentials
    settings = replace(Settings(), api_token="conn-tok", database_url=f"sqlite:///{tmp_path}/conn.db")
    headers = {"Authorization": "Bearer conn-tok"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        mat0 = web.get("/api/connections/matrix", headers=headers).json()
        assert len(mat0["providers"]) == 5
        saved = web.post(
            "/api/connections/alpaca/credentials",
            headers=headers,
            json={"api_key": "AK-CONN-99", "api_secret": "AS-CONN-99", "feed": "iex"},
        )
        assert saved.status_code == 200
        assert "AK-CONN-99" not in saved.text
        assert saved.json()["present"] is True
        mat1 = web.get("/api/connections/matrix", headers=headers).json()
        us_md = next(p for p in mat1["providers"] if p["id"] == "alpaca_market_data")
        assert us_md["credential_present"] is True


@pytest.mark.skipif(
    not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_API_SECRET")),
    reason="NOT VERIFIED in sandbox: real owner ALPACA_API_KEY / ALPACA_API_SECRET not present in environment",
)
def test_optional_real_owner_alpaca_readonly_quote():
    res = verify_alpaca_market_data(
        os.environ["ALPACA_API_KEY"],
        os.environ["ALPACA_API_SECRET"],
        feed=os.getenv("ALPACA_DATA_FEED", "iex"),
        symbol="AAPL",
    )
    assert res["authenticated"] is True
    assert res["real_order_api_used"] is False
