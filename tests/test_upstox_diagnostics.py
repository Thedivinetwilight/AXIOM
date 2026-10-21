"""Regression tests for sanitized Upstox feed diagnostics.

Every network interaction in this module uses the loopback protocol fixture;
no external Upstox account, credential, or order endpoint is involved.
"""

import asyncio
import json
from types import SimpleNamespace

from app.data.upstox_proto.MarketDataFeed_pb2 import FeedResponse
from app.data.upstox_stream import UpstoxStream
from tests.test_upstox_live import (
    KEY,
    SYMBOL,
    MarketFeedServer,
    bucket,
    ltpc_frame,
    make_provider,
    market_info_frame,
    start_authorize_server,
)


def _multi_market_info_frame(statuses: dict[str, int]) -> bytes:
    response = FeedResponse(type=2)
    for segment, status in statuses.items():
        response.marketInfo.segmentStatus[segment] = status
    return response.SerializeToString()


def test_loopback_feed_separates_messages_completed_suppressed_and_delivered():
    """An aggregate NORMAL_OPEN can coexist with a closed NSE instrument."""

    async def scenario():
        feed_server = MarketFeedServer()
        delivered = asyncio.Event()
        base = bucket(0)

        async def plan(ws, _connection_index):
            # BSE is open, so the aggregate is NORMAL_OPEN, but NSE is closed;
            # the first NSE candle must be withheld.
            await ws.send(_multi_market_info_frame({"NSE": 3, "BSE": 2}))
            await ws.send(ltpc_frame(100.0, base + 30))
            await ws.send(market_info_frame("NSE", 2))
            await ws.send(ltpc_frame(101.0, base + 90))
            await asyncio.sleep(5)  # hold open well past assertions/cleanup

        await feed_server.start(plan)
        auth_server, auth_port = await start_authorize_server(feed_server.port, [])
        provider = make_provider(
            f"http://127.0.0.1:{auth_port}/authorize", idle_timeout=1.0)
        seen = []

        async def consume():
            async for bar in provider.stream(provider.symbols, "1m"):
                seen.append(bar)
                delivered.set()

        consumer = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(delivered.wait(), timeout=4)
            metrics = provider.metrics()
            assert len(seen) == 1
            assert metrics["websocket_connected_once"] is True
            assert metrics["subscription_sent"] is True
            assert metrics["websocket_messages_received"] == 4
            assert metrics["websocket_binary_messages_received"] == 4
            assert metrics["completed_candles"] == 2
            assert metrics["suppressed_candles"] == 1
            assert metrics["delivered_candles"] == 1
            assert metrics["market_status"] == "NORMAL_OPEN"
            assert metrics["market_open"] is True
            suppression = metrics["last_suppression"]
            assert suppression["instrument_key"] == KEY
            assert suppression["symbol"] == SYMBOL
            assert suppression["market_status"] == "NORMAL_OPEN"
            assert suppression["market_open"] is False
            assert suppression["market_open_segment"] == "NSE"
            assert suppression["market_open_segment_status"] == "NORMAL_CLOSE"
            assert suppression["segment_statuses"] == {
                "NSE": "NORMAL_CLOSE", "BSE": "NORMAL_OPEN"
            }
            assert suppression["candle_timestamp"]
            assert provider.last_suppression["market_open"] is False
            assert feed_server.subscriptions[0]["data"] == {
                "mode": "ltpc", "instrumentKeys": [KEY]
            }
        finally:
            await provider.close()
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
            await feed_server.stop()
            auth_server.close()
            await auth_server.wait_closed()

    asyncio.run(scenario())


def test_upstox_test_snapshots_connected_suppressed_stream_before_cleanup(
    monkeypatch, capsys
):
    """A CLI deadline is not mislabeled as a failed WebSocket handshake."""
    import app.__main__ as cli
    import app.data.upstox_auth as auth

    async def verify(*_args, **_kwargs):
        return SimpleNamespace(
            verdict="feed_ok",
            public_view=lambda: {
                "verdict": "feed_ok", "http_status": 200, "authenticated": True
            },
        )

    class ConnectedSuppressedProvider:
        mode = "ltpc"
        timeframe = "1m"
        symbols = [SYMBOL]
        status = "DISCONNECTED"

        def __init__(self):
            self.closed = False
            self.connected_once = False

        def metrics(self):
            return {
                "instrument_keys": [KEY],
                "connection_status": self.status,
                "websocket_connected_once": self.connected_once,
                "subscription_sent": self.connected_once,
                "websocket_messages_received": 2 if self.connected_once else 0,
                "authenticated": self.connected_once,
                "completed_candles": 1 if self.connected_once else 0,
                "suppressed_candles": 1 if self.connected_once else 0,
                "delivered_candles": 0,
                "market_status": "NORMAL_OPEN",
                "market_open": False,
                "market_open_segment": "NSE",
                "feed_mode": self.mode,
                "timeframe": self.timeframe,
                "last_connection_error": None,
            }

        async def stream(self, _symbols, _timeframe):
            self.connected_once = True
            self.status = "CONNECTED"
            await asyncio.Event().wait()
            if False:  # make this an async generator; no bar is ever yielded
                yield None

        async def close(self):
            self.closed = True
            self.status = "DISCONNECTED"

    provider = ConnectedSuppressedProvider()
    monkeypatch.setattr(cli, "_load_settings", lambda: SimpleNamespace(
        market_data_access_token="synthetic-secret",
        market_data_authorize_url="https://unit.invalid/authorize",
    ))
    monkeypatch.setattr(cli, "_provider_or_exit", lambda _settings: provider)
    monkeypatch.setattr(auth, "verify_feed_credential", verify)

    code = cli.cmd_upstox_test(SimpleNamespace(timeout=0.02))
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert code == 1
    assert output["ok"] is False
    assert output["websocket"] == "connected_no_candle_before_deadline"
    assert output["websocket_status"] == "CONNECTED"
    assert output["websocket_connected_once"] is True
    assert output["websocket_messages_received"] == 2
    assert output["completed_candles"] == 1
    assert output["suppressed_candles"] == 1
    assert output["delivered_candles"] == output["candles"] == 0
    assert "frames" not in output
    assert provider.closed and provider.status == "DISCONNECTED"
    assert "synthetic-secret" not in captured.out


def test_close_code_diagnostic_is_sanitized():
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    exc = ConnectionClosedError(Close(1008, "private provider detail"), None)
    detail = UpstoxStream._connection_error_diagnostics(exc)
    assert detail["error_type"] == "ConnectionClosedError"
    assert detail["close_code"] == 1008
    assert "reason" not in detail and "private provider detail" not in repr(detail)
    assert "url" not in detail and "token" not in detail


def test_handshake_status_diagnostic_excludes_response_body():
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Headers, Response

    response = Response(
        403,
        "Forbidden",
        Headers({"Authorization": "must-not-appear"}),
        body=b"access token=must-not-appear",
    )
    detail = UpstoxStream._connection_error_diagnostics(InvalidStatus(response))
    assert detail["error_type"] == "InvalidStatus"
    assert detail["handshake_http_status"] == 403
    rendered = repr(detail).lower()
    assert "must-not-appear" not in rendered
    assert "authorization" not in rendered
    assert "body" not in detail and "headers" not in detail
