"""V0.6.3 tests: behavioral credential-type verification BEFORE connecting.

Per the official analytics-token documentation (verified 2026-09-22), the
long-lived Analytics Token covers read-only GET APIs including a 'Websocket'
category — but feed access must be PROVEN with the actual token, never
assumed. Tokens are opaque, so verification is the documented GET authorize
probe. These tests pin the verdict logic, the no-connection-on-failure CLI
behavior, and secret hygiene.
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.config.settings import Settings
from app.data.upstox_auth import (
    GUIDANCE_FORBIDDEN,
    GUIDANCE_REJECTED,
    CredentialCheck,
    verify_feed_credential,
)
from app.data.upstox_stream import UpstoxStream
from tests.test_upstox_live import KEY, TOKEN


def async_check(token, transport, **kwargs):
    check = verify_feed_credential(
        token, authorize_url=kwargs.pop("authorize_url", "https://api.upstox.com/v3/feed/market-data-feed/authorize"),
        transport=transport, **kwargs,
    )
    return asyncio.run(check)


def test_verify_feed_credential_verdicts():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(
            200, json={"data": {"authorized_redirect_uri": "wss://feed.upstox.com/xyz"}}
        )

    check = async_check(TOKEN, httpx.MockTransport(ok))
    assert isinstance(check, CredentialCheck)
    assert check.verdict == "feed_ok" and check.authenticated is True
    assert check.http_status == 200 and "WebSocket has not yet been tested" in check.guidance

    def bad_shape(request):
        return httpx.Response(200, json={"data": {}})

    assert async_check(TOKEN, httpx.MockTransport(bad_shape)).verdict == "unexpected_response"

    def rejected(request):
        return httpx.Response(401, json={"status": "error"})

    check = async_check(TOKEN, httpx.MockTransport(rejected))
    assert check.verdict == "rejected" and check.authenticated is False
    assert "Analytics Token" in check.guidance

    def forbidden(request):
        return httpx.Response(403, json={"status": "error"})

    check = async_check(TOKEN, httpx.MockTransport(forbidden))
    assert check.verdict == "forbidden" and "OAuth login access token" in check.guidance

    def down(request):
        raise httpx.ConnectError("no route")

    check = async_check(TOKEN, httpx.MockTransport(down))
    # 3.2.1: granular verdicts distinguish TCP/DNS failure from a generic error.
    assert check.verdict == "dns_or_connect_failure" and check.authenticated is False

    def timed_out(request):
        raise httpx.ConnectTimeout("timed out")

    check = async_check(TOKEN, httpx.MockTransport(timed_out))
    assert check.verdict == "timeout" and check.authenticated is False

    empty = asyncio.run(
        verify_feed_credential("", authorize_url="https://api.upstox.com/v3/feed/market-data-feed/authorize")
    )
    assert empty.verdict == "invalid_input" and empty.authenticated is False


def test_credential_check_never_contains_token():
    def ok(request):
        return httpx.Response(
            200, json={"data": {"authorized_redirect_uri": "wss://feed.upstox.com/xyz?code=secret-code"}}
        )

    for verdict_case in (
        httpx.MockTransport(ok),
        httpx.MockTransport(lambda r: httpx.Response(401, json={"errors": [TOKEN]})),
    ):
        check = async_check("super-secret-token-value", verdict_case)
        view = json.dumps(check.public_view()) + repr(check)
        assert "super-secret-token-value" not in view
        assert "secret-code" not in view


def test_auth_error_includes_credential_guidance():
    provider = UpstoxStream(
        TOKEN, [KEY],
        http_transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"status": "error"})),
        max_backoff=0.01,
    )

    async def scenario():
        from app.data.upstox_stream import UpstoxAuthError

        with pytest.raises(UpstoxAuthError) as excinfo:
            async for _ in provider.stream(provider.symbols, "1m"):
                break
        message = str(excinfo.value)
        assert "401" in message and "Analytics Token" in message
        assert TOKEN not in message  # guidance without secrets

    asyncio.run(scenario())
    assert provider.status == "AUTH_FAILED" and provider.reconnects == 0


def test_upstox_test_preflight_blocks_connection_on_rejected_credential(capsys, monkeypatch):
    """Rejected credential → NO WebSocket attempt; status is not a network failure."""
    from app.data import upstox_diagnostic

    class One401(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(
                401, json={"status": "error", "errors": [{"code": "invalid_token"}]},
                request=request,
            )

    transport = One401()
    original = upstox_diagnostic.run_upstox_diagnostic

    async def mocked_diagnostic(token, **kwargs):
        return await original(
            token,
            authorize_url="http://local.test/authorize",
            http_transport=transport,
            websocket_connector=lambda *_args, **_kwargs: pytest.fail(
                "WebSocket must not be attempted after HTTP 401"),
            **kwargs,
        )

    monkeypatch.setattr(upstox_diagnostic, "run_upstox_diagnostic", mocked_diagnostic)
    monkeypatch.setenv("DATA_SOURCE", "upstox")
    monkeypatch.setenv("MARKET_DATA_SYMBOLS", KEY)
    monkeypatch.setenv("MARKET_DATA_ACCESS_TOKEN", TOKEN)
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DEFAULT_TIMEFRAME", raising=False)
    monkeypatch.delenv("MARKET_DATA_TIMEFRAME", raising=False)
    # This override is intentionally ignored by the diagnostic: it always
    # targets the documented Upstox endpoint, except for the private test seam.
    monkeypatch.setenv("MARKET_DATA_AUTHORIZE_URL", "http://not-used.invalid/")

    from app.__main__ import main

    with pytest.raises(SystemExit) as excinfo:
        main(["upstox-test", "--websocket"])
    assert excinfo.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["https_reachability"]["status"] == "reachable"
    assert summary["credential_authorization"]["status"] == "rejected_401"
    assert summary["credential_check"]["verdict"] == "rejected"
    assert summary["websocket"] == "not_tried"
    assert summary["websocket_check"]["handshake"] == "not_attempted"
    assert summary["ok"] is False and TOKEN not in json.dumps(summary)
    assert transport.calls == 1


def test_settings_version_is_063_and_defaults_unchanged():
    settings = replace(Settings(), data_source="upstox", default_timeframe="1m",
                       market_data_timeframe="1m", market_data_symbols=KEY,
                       market_data_access_token="tok")
    assert settings.market_data_authorize_url.startswith("https://api.upstox.com/v3/")
    assert GUIDANCE_REJECTED != GUIDANCE_FORBIDDEN
