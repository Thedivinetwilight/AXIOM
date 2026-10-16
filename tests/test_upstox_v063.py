"""V0.6.3 tests: behavioral credential-type verification BEFORE connecting.

Per the official analytics-token documentation (verified 2026-09-22), the
long-lived Analytics Token covers read-only GET APIs including a 'Websocket'
category — but feed access must be PROVEN with the actual token, never
assumed. Tokens are opaque, so verification is the documented GET authorize
probe. These tests pin the verdict logic, the no-connection-on-failure CLI
behavior, and secret hygiene.
"""

import asyncio
from dataclasses import replace
import json
import os

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
            200, json={"data": {"authorized_redirect_uri": "wss://feed.example/xyz"}}
        )

    check = async_check(TOKEN, httpx.MockTransport(ok))
    assert isinstance(check, CredentialCheck)
    assert check.verdict == "feed_ok" and check.authenticated is True
    assert check.http_status == 200 and "Safe to open the WebSocket" in check.guidance

    def bad_shape(request):
        return httpx.Response(200, json={"data": {}})

    assert async_check(TOKEN, httpx.MockTransport(bad_shape)).verdict == "unexpected_response"

    def rejected(request):
        return httpx.Response(401, json={"status": "error"})

    check = async_check(TOKEN, httpx.MockTransport(rejected))
    assert check.verdict == "rejected" and check.authenticated is False
    assert "Analytics Token" in check.guidance and check == check  # dataclass eq ok

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
            200, json={"data": {"authorized_redirect_uri": "wss://feed.example/xyz?code=secret-code"}}
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


def test_upstox_test_preflight_blocks_connection_on_rejected_credential(capsys):
    """Rejected credential → NO WebSocket connection attempt (verdict first)."""
    import socket
    import threading

    def serve_401(ready, accepted, holder):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        holder.append(server.getsockname()[1])
        ready.set()
        connection, _ = server.accept()
        connection.recv(4096)
        connection.sendall(
            b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\n"
            b"Connection: close\r\nContent-Length: 34\r\n\r\n"
            b'{"status":"error","errors":[{"x":1}]}'
        )
        connection.close()
        server.close()
        accepted.set()

    ready, accepted, holder = threading.Event(), threading.Event(), []
    worker = threading.Thread(target=serve_401, args=(ready, accepted, holder), daemon=True)
    worker.start()
    assert ready.wait(timeout=5)
    auth_port = holder[0]

    saved = {k: os.environ.get(k) for k in (
        "DATA_SOURCE", "MARKET_DATA_SYMBOLS", "MARKET_DATA_ACCESS_TOKEN",
        "MARKET_DATA_AUTHORIZE_URL", "DEFAULT_TIMEFRAME", "MARKET_DATA_TIMEFRAME",
        "UPSTOX_ACCESS_TOKEN", "MARKET_DATA_TOKEN",
    )}
    os.environ["DATA_SOURCE"] = "upstox"
    os.environ["MARKET_DATA_SYMBOLS"] = KEY
    os.environ["MARKET_DATA_ACCESS_TOKEN"] = TOKEN
    os.environ["MARKET_DATA_AUTHORIZE_URL"] = (
        f"http://127.0.0.1:{auth_port}/v3/feed/market-data-feed/authorize"
    )
    for name in ("DEFAULT_TIMEFRAME", "MARKET_DATA_TIMEFRAME"):
        os.environ.pop(name, None)
    try:
        from app.__main__ import main

        with pytest.raises(SystemExit) as excinfo:
            main(["upstox-test"])
        assert excinfo.value.code == 1
        summary = json.loads(capsys.readouterr().out)
        assert summary["credential_check"]["verdict"] == "rejected"
        assert summary["websocket"] == "not_tried"  # never connected
        assert summary["ok"] is False and TOKEN not in json.dumps(summary)
        assert accepted.wait(timeout=5)  # exactly one authorize probe was made
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        worker.join(timeout=5)


def test_settings_version_is_063_and_defaults_unchanged():
    settings = replace(Settings(), data_source="upstox", default_timeframe="1m",
                       market_data_timeframe="1m", market_data_symbols=KEY,
                       market_data_access_token="tok")
    assert settings.market_data_authorize_url.startswith("https://api.upstox.com/v3/")
    assert GUIDANCE_REJECTED != GUIDANCE_FORBIDDEN
