"""V0.6.1 live market-data integration tests — LOCAL PROTOCOL ONLY.

These tests run a real loopback HTTP authorize endpoint and a real loopback
WebSocket server that speak the documented Upstox V3 wire protocol, decoded
with the OFFICIAL protobuf schema shipped in app/data/upstox_proto. Candles
flow through the unchanged strategy/risk/PaperBroker pipeline.

No test in this file contacts an external network and no real credential is
used: the token constant below is a synthetic dummy value.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.api.server import create_app
from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings
from app.data.factory import stream_provider
from app.data.models import Candle
from app.data.upstox_proto.MarketDataFeed_pb2 import FeedResponse
from app.data.upstox_stream import (
    AUTHORIZE_URL,
    INSTRUMENT_KEY,
    UpstoxAuthError,
    UpstoxStream,
    from_symbol,
    to_symbol,
)
from app.database.database import Database
from app.database.repository import Repository
from app.paper_trading.broker import FutureRealBroker, PaperBroker
from app.paper_trading.simulator import BotState, PaperSimulator
from app.strategies import create_strategy

KEY = "NSE_EQ|INE002A01018"  # Reliance Industries instrument-key FORM (Upstox docs)
SYMBOL = "NSE_EQ-INE002A01018"  # project-internal display form
TOKEN = "dummy-local-test-token"  # synthetic value; never a real credential
PRICES = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5]


def live_config(**overrides):
    values = dict(
        initial_capital=100_000,
        commission=0,
        slippage=0,
        spread=0,
        position_size=0.1,
        stop_loss=None,
        take_profit=None,
        max_participation=1,
    )
    values.update(overrides)
    return ExecutionConfig(**values)


def past_base():
    """Epoch-aligned minute bucket ~6 minutes in the past (UTC hour alignment)."""
    now = int(time.time()) - 400
    return now - now % 60


def bucket(index: int) -> int:
    return past_base() + index * 60


def live_candle(index: int, price: float, *, volume=10_000, symbol=SYMBOL):
    stamp = datetime.fromtimestamp(bucket(index), timezone.utc)
    return Candle(stamp, price, price + 0.25, price - 0.25, price + 0.1, volume, symbol)


def ltpc_frame(price: float, ts_seconds: int, *, qty=10_000, initial=False) -> bytes:
    response = FeedResponse(type=0 if initial else 1, currentTs=int(ts_seconds * 1000))
    feed = response.feeds[KEY]
    feed.ltpc.ltp = float(price)
    feed.ltpc.ltt = int(ts_seconds * 1000)
    feed.ltpc.ltq = int(qty)
    return response.SerializeToString()


def ohlc_frame(index: int, o, h, low, c, v, *, interval="1m") -> bytes:
    response = FeedResponse(type=1, currentTs=int((bucket(index) + 60) * 1000))
    bar = response.feeds[KEY].fullFeed.marketFF.marketOHLC.ohlc.add()
    bar.interval = interval
    bar.ts = bucket(index)
    bar.open, bar.high, bar.low, bar.close, bar.vol = o, h, low, c, int(v)
    return response.SerializeToString()


def market_info_frame(segment: str = "NSE", status: int = 2, *, ts_seconds=None) -> bytes:
    """Official market_info frame (segmentStatus map; 2 == NORMAL_OPEN)."""
    response = FeedResponse(
        type=2, currentTs=int((ts_seconds if ts_seconds is not None else time.time()) * 1000)
    )
    response.marketInfo.segmentStatus[segment] = status
    return response.SerializeToString()


async def start_authorize_server(ws_port: int, seen_headers: list, *, status=200):
    """Minimal real HTTP server returning the documented authorize response."""

    async def handler(reader, writer):
        raw = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
        seen_headers.append(raw)
        if status != 200:
            body = b'{"status":"error","errors":[{"errorCode":"401"}]}'
            head = f"HTTP/1.1 {status} Denied\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n"
        else:
            body = json.dumps(
                {
                    "status": "success",
                    "data": {"authorized_redirect_uri": f"ws://127.0.0.1:{ws_port}"},
                }
            ).encode()
            head = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n"
        writer.write(head.encode() + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class MarketFeedServer:
    """Real loopback WebSocket server speaking the documented feed protocol."""

    def __init__(self, ssl_context=None):
        self.subscriptions: list[dict] = []
        self.connections = 0
        self.server = None
        self.port = None
        self.ssl_context = ssl_context  # optional real-TLS loopback (e2e driver)

    async def start(self, plan):
        from websockets.asyncio.server import serve

        outer = self

        async def handler(ws):
            outer.connections += 1
            frame = await asyncio.wait_for(ws.recv(), timeout=5)
            assert isinstance(frame, bytes), "subscription frame must be binary"
            outer.subscriptions.append(json.loads(frame.decode()))
            await plan(ws, outer.connections)

        self.server = await serve(handler, "127.0.0.1", 0, ssl=self.ssl_context)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


def make_provider(authorize_url: str, **kwargs) -> UpstoxStream:
    defaults = dict(max_backoff=0.05, stale_after=30, idle_timeout=0.25,
                    ws_url_schemes=("ws", "wss"))  # loopback fixture uses plain ws://
    defaults.update(kwargs)
    return UpstoxStream(TOKEN, [KEY], timeframe="1m", mode="ltpc",
                        authorize_url=authorize_url, **defaults)


# --------------------------------------------------------------------------- auth


def test_missing_token_rejected_before_any_network():
    with pytest.raises(ValueError, match="access token"):
        UpstoxStream("", [KEY])
    settings = replace(
        Settings(),
        data_source="upstox",
        default_timeframe="1m",
        market_data_timeframe="1m",
        market_data_symbols=KEY,
        market_data_access_token="",
    )
    with pytest.raises(ValueError, match="access token"):
        stream_provider(settings, "upstox")


def test_invalid_keys_mode_and_timeframe_rejected():
    with pytest.raises(ValueError, match="instrument key"):
        UpstoxStream(TOKEN, ["RELIANCE"])
    with pytest.raises(ValueError, match="mode"):
        UpstoxStream(TOKEN, [KEY], mode="order")
    with pytest.raises(ValueError, match="timeframe"):
        UpstoxStream(TOKEN, [KEY], timeframe="4h")
    with pytest.raises(ValueError, match="1m/30m"):
        UpstoxStream(TOKEN, [KEY], mode="full", timeframe="5m")
    assert to_symbol(KEY) == SYMBOL and from_symbol(SYMBOL) == KEY
    assert INSTRUMENT_KEY.fullmatch("NSE_INDEX|Nifty 50")  # provider key format is honored
    with pytest.raises(ValueError, match="not usable as an internal symbol"):
        UpstoxStream(TOKEN, ["NSE_INDEX|Nifty 50"])  # spaced index keys stay unsupported


def test_authorize_http_401_fails_closed_without_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Authorization"])
        return httpx.Response(401, json={"status": "error", "errors": [{"errorCode": "401"}]})

    provider = UpstoxStream(
        TOKEN, [KEY], http_transport=httpx.MockTransport(handler), max_backoff=0.01
    )

    async def scenario():
        with pytest.raises(UpstoxAuthError) as excinfo:
            await anext(provider.stream([SYMBOL], "1m"))
        assert "401" in str(excinfo.value)
        assert TOKEN not in str(excinfo.value)
        assert b"errorCode" not in str(excinfo.value).encode()

    asyncio.run(scenario())
    assert provider.status == "AUTH_FAILED" and provider.reconnects == 0
    assert calls == [f"Bearer {TOKEN}"]  # exact documented header, single attempt


def test_authorize_malformed_or_non_wss_body_fails_closed():
    def bad_json(request):
        return httpx.Response(200, text="not json")

    def no_uri(request):
        return httpx.Response(200, json={"status": "success", "data": {}})

    def plain_uri(request):
        return httpx.Response(200, json={"data": {"authorized_redirect_uri": "http://x"}})

    for transport in (httpx.MockTransport(bad_json), httpx.MockTransport(no_uri),
                      httpx.MockTransport(plain_uri)):
        provider = UpstoxStream(TOKEN, [KEY], http_transport=transport)
        with pytest.raises(UpstoxAuthError):
            asyncio.run(anext(provider.stream([SYMBOL], "1m")).aclose() if False else _first(provider))
        assert provider.status == "AUTH_FAILED"


async def _first(provider):
    async for _ in provider.stream([SYMBOL], "1m"):
        break


# ------------------------------------------------------------------ data quality


def test_incomplete_tick_candle_is_never_emitted_early():
    builder_stream = UpstoxStream(TOKEN, [KEY], timeframe="1m")._builder
    now = time.time()
    ts = now - now % 60 + 5  # inside the current, still-forming minute
    builder_stream.ingest_tick(SYMBOL, ts, 100.0, 10, received_at=now)
    assert builder_stream.collect(now=now) == []
    completed = builder_stream.collect(now=now + 120)
    assert len(completed) == 1
    bar = completed[0]
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100.0, 100.0, 100.0, 100.0, 10)
    assert bar.available_at("1m") <= datetime.fromtimestamp(now + 120, timezone.utc)


def test_duplicate_and_out_of_order_ticks_are_dropped_not_double_counted():
    builder_stream = UpstoxStream(TOKEN, [KEY], timeframe="1m")._builder
    base = past_base()
    builder_stream.ingest_tick(SYMBOL, base + 50, 100.0, 10, received_at=time.time())
    builder_stream.ingest_tick(SYMBOL, base + 50, 100.0, 10, received_at=time.time())
    builder_stream.ingest_tick(SYMBOL, base + 10, 90.0, 10, received_at=time.time())
    builder_stream.ingest_tick(SYMBOL, base + 110, 101.0, 5, received_at=time.time())
    assert builder_stream.duplicates == 1
    assert builder_stream.out_of_order == 1
    completed = builder_stream.collect(now=base + 400)
    assert len(completed) == 2  # bucket0 and bucket1 windows have both closed
    bar = completed[0]
    assert (bar.low, bar.high) == (90.0, 100.0)  # late tick refined extremes only
    assert bar.volume == 10  # retransmitted/late quantity never double counts


def test_official_interval_bars_complete_on_supersede():
    builder_stream = UpstoxStream(TOKEN, [KEY], timeframe="1m", mode="full")._builder
    now = time.time()
    current = now - now % 60
    builder_stream.ingest_interval(SYMBOL, "1m", current, 100, 101, 99.5, 100.5, 500, received_at=now)
    assert builder_stream.collect(now=now) == []  # official bar still forming
    builder_stream.ingest_interval(SYMBOL, "1m", current, 100, 101.5, 99.5, 100.8, 600, received_at=now)
    builder_stream.ingest_interval(SYMBOL, "1m", current, 100, 101.5, 99.5, 100.8, 600, received_at=now)
    assert builder_stream.duplicates == 1  # identical snapshot ignored
    assert builder_stream.collect(now=now) == []
    builder_stream.ingest_interval(SYMBOL, "1m", current + 60, 100.8, 102, 100, 101.9, 700, received_at=now)
    bars = builder_stream.collect(now=now)  # newer open_ts completes the previous bar
    assert len(bars) == 1
    bar = bars[0]
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100.0, 101.5, 99.5, 100.8, 600)


def test_full_mode_decode_uses_official_bars_only_and_counts_malformed():
    provider = UpstoxStream(TOKEN, [KEY], timeframe="1m", mode="full")
    bars = provider._decode(ohlc_frame(0, 100, 101, 99.5, 100.5, 500))  # past bucket
    assert len(bars) == 1  # window already closed → completed immediately
    assert bars[0].close == 100.5 and bars[0].volume == 500
    assert provider._decode(ltpc_frame(100.0, past_base() + 30)) == []
    assert provider._builder.ticks == 0  # ltpc ticks never double-build in full mode
    assert provider._decode(b"not-a-protobuf-frame") == []
    assert provider._decoder.malformed == 1  # malformed frames never kill the stream


def test_unknown_instrument_and_invalid_fields_are_ignored_safely():
    provider = UpstoxStream(TOKEN, [KEY], timeframe="1m", mode="ltpc")
    other = FeedResponse(type=1, currentTs=1)
    feed = other.feeds["NSE_EQ|UNSUBSCRIBED000"]
    feed.ltpc.ltp, feed.ltpc.ltt = 50.0, int(past_base() * 1000)
    assert provider._decode(other.SerializeToString()) == []
    now = time.time()
    builder_stream = provider._builder
    builder_stream.ingest_tick(SYMBOL, 0, 100.0, 1, received_at=now)  # uninitialized ts
    builder_stream.ingest_tick(SYMBOL, past_base(), 0.0, 1, received_at=now)  # invalid price
    builder_stream.ingest_tick(SYMBOL, past_base(), 100.0, -5, received_at=now)  # bad volume
    assert builder_stream.invalid_timestamps == 1 and builder_stream.malformed == 2
    assert builder_stream.ticks == 0


def test_gap_detection_and_controller_pause():
    provider = UpstoxStream(TOKEN, [KEY], timeframe="1m")
    builder_stream = provider._builder
    builder_stream.ingest_tick(SYMBOL, bucket(0) + 1, 100.0, 10, received_at=time.time())
    builder_stream.ingest_tick(SYMBOL, bucket(2) + 1, 101.0, 10, received_at=time.time())
    bars = builder_stream.collect(now=bucket(3))
    stamps = [b.timestamp for b in bars]
    assert stamps == [
        datetime.fromtimestamp(bucket(0), timezone.utc),
        datetime.fromtimestamp(bucket(2), timezone.utc),
    ]
    assert builder_stream.gaps == 1
    assert provider.metrics()["data_gap_detected"] is True

    async def scenario():
        broker = PaperBroker(live_config(), timeframe="1m")
        sim = PaperSimulator(broker)
        sim.provider, sim.strategy = provider, create_strategy("momentum", {"period": 2, "threshold": 0.0})
        sim.symbols, sim.state = [SYMBOL], BotState.RUNNING
        sim.process_bar(live_candle(0, 100.0))
        sim.process_bar(live_candle(1, 100.5))
        sim.process_bar(live_candle(3, 101.5))  # bucket(2) missing → gap
        assert sim.state == BotState.PAUSED
        assert sim.error and "Data gap" in sim.error
        active_before = [o["id"] for o in broker.get_orders() if o["status"] == "NEW"]
        sim.process_bar(live_candle(4, 102.0))  # paused → HOLD, no new strategy orders
        new_ids = [o["id"] for o in broker.get_orders() if o["status"] == "NEW"]
        assert not set(new_ids) - set(active_before)
        assert sim.signals[SYMBOL]["signal"] == "HOLD"

    asyncio.run(scenario())


def test_stale_feed_detection():
    builder_stream = UpstoxStream(TOKEN, [KEY], timeframe="1m", stale_after=10)._builder
    now = time.time()
    assert builder_stream.stale(now=now) is False  # no activity yet → not stale
    builder_stream.ingest_tick(SYMBOL, bucket(0) + 1, 100.0, 1, received_at=now - 30)
    assert builder_stream.stale(now=now) is True
    provider = UpstoxStream(TOKEN, [KEY], timeframe="1m", stale_after=10)
    assert provider.metrics()["stale_feed"] is False


# ------------------------------------------------------- real-socket integration


def test_live_pipeline_over_real_sockets_reconnect_heartbeat_duplicate(monkeypatch):
    """Full documented protocol over real loopback TCP: authorize → binary sub →
    protobuf ticks → completed candles → strategy → PAPER order, then a forced
    disconnect → re-auth → resubscribe → duplicate suppression, plus the idle
    heartbeat path. No external network, no real credentials."""
    # Deterministic reconnect timing: remove the random backoff jitter only.
    monkeypatch.setattr("app.data.upstox_stream.random", lambda: 0.0)

    async def scenario():
        feed_server = MarketFeedServer()

        async def plan_first(ws, connection_index):
            await ws.send(market_info_frame())  # market OPEN before any tick
            await ws.send(ltpc_frame(100.0, bucket(0) + 30, initial=True))
            for index, price in enumerate(PRICES):
                await ws.send(ltpc_frame(price, bucket(index) + 30))
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.6)  # idle > idle_timeout → client heartbeat cycle
            await ws.close(code=1011)  # force reconnect

        async def plan_second(ws, connection_index):
            # Reconnect: market status + last tick replay (duplicate suppression).
            await ws.send(market_info_frame())
            await ws.send(ltpc_frame(PRICES[-1], bucket(5) + 30, initial=True))
            await asyncio.sleep(1.0)

        await feed_server.start(_plan_router(plan_first, plan_second))
        auth_seen: list = []
        auth_server, auth_port = await start_authorize_server(feed_server.port, auth_seen)
        provider = make_provider(f"http://127.0.0.1:{auth_port}/v3/feed/market-data-feed/authorize")
        broker = PaperBroker(live_config(), timeframe="1m")
        sim = PaperSimulator(broker)
        try:
            await sim.start(
                provider,
                create_strategy("momentum", {"period": 2, "threshold": 0.0}),
                [SYMBOL],
            )
            deadline = time.time() + 15
            while time.time() < deadline and not (
                sim.processed >= len(PRICES) and broker.get_orders()
            ):
                await asyncio.sleep(0.05)
            await asyncio.sleep(1.5)  # reconnect + duplicate handling window

            metrics = provider.metrics()
            orders = broker.get_orders()
            status = sim.status()

            assert sim.state == BotState.RUNNING
            assert sim.processed >= len(PRICES)
            assert provider.reconnects == 1
            assert feed_server.connections == 2
            assert len(auth_seen) >= 2  # re-authorized on every reconnect (real HTTP)
            assert all(f"Bearer {TOKEN}" in head for head in auth_seen)
            assert len(feed_server.subscriptions) == 2
            for subscription in feed_server.subscriptions:
                assert subscription["method"] == "sub"
                assert subscription["data"]["mode"] == "ltpc"
                assert subscription["data"]["instrumentKeys"] == [KEY]
                assert subscription["guid"]
            buys = [o for o in orders if o["side"] == "BUY"]
            assert len(orders) == 1 and len(buys) == 1
            assert orders[0]["status"] == "FILLED" and len(broker.fills) == 1
            assert metrics["authenticated"] is True and metrics["subscribed"] is True
            assert metrics["connection_status"] == "CONNECTED"
            assert metrics["market_status"] == "NORMAL_OPEN" and metrics["market_open"] is True
            assert metrics["last_candle_time"] is not None
            assert metrics["feed_counters"]["completed_candles"] >= len(PRICES)
            assert metrics["feed_counters"]["duplicates"] >= 1
            assert metrics["feed_latency_ms"]["messages"] >= len(PRICES)
            assert status["paper_mode"] is True and status["mode"] == "PAPER"
            assert status["live_trading"] is False
            assert status["processing_latency_ms"]["bars"] >= len(PRICES)
            assert status["signals"][SYMBOL]["signal"] in {"BUY", "HOLD"}
            assert broker.get_positions(), "paper position expected after rising feed"
            dump = json.dumps(status) + json.dumps(metrics) + repr(provider)
            assert TOKEN not in dump
            await sim.stop()
            assert sim.state == BotState.STOPPED
        finally:
            await provider.close()
            await sim.stop()
            await feed_server.stop()
            auth_server.close()
            await auth_server.wait_closed()

    asyncio.run(scenario())


def _plan_router(first, second):
    async def plan(ws, connection_index):
        if connection_index == 1:
            await first(ws, connection_index)
        else:
            await second(ws, connection_index)

    return plan


def test_production_wss_uses_verified_default_tls(monkeypatch):
    """ssl must never be None for wss://; the library default context verifies TLS."""
    import websockets.asyncio.client

    def authorize_ok(request):
        return httpx.Response(
            200, json={"data": {"authorized_redirect_uri": "wss://demo-feed.invalid/feeds"}}
        )

    provider = UpstoxStream(
        TOKEN, [KEY], http_transport=httpx.MockTransport(authorize_ok), max_backoff=0.01
    )
    captured = {}

    def connect(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        provider.closed = True  # terminate the stream after the first connection

        class Socket:
            async def send(self, frame):
                pass

            async def recv(self):
                raise OSError("stop")

            async def close(self):
                pass

            async def __aenter__(self):
                return Socket()

            async def __aexit__(self, *args):
                return False

        return Socket()

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)

    async def scenario():
        async for _ in provider.stream([SYMBOL], "1m"):
            break  # the fake socket never yields candles

    asyncio.run(scenario())
    assert captured["uri"].startswith("wss://")
    assert captured["ssl"] is True, "wss must use the verified default TLS context"


# ------------------------------------------------------- trading/restart safety


def test_restart_restores_account_without_duplicate_orders(tmp_path):
    url = "sqlite:///" + str(tmp_path / "live_paper.db")
    strategy = create_strategy("momentum", {"period": 2, "threshold": 0.0})

    def attach(sim: PaperSimulator):
        sim.provider = UpstoxStream(TOKEN, [KEY], timeframe="1m")
        sim.strategy = strategy
        sim.symbols, sim.state = [SYMBOL], BotState.RUNNING

    db = Database(url)
    repo = Repository(db)
    broker = PaperBroker(live_config(initial_capital=1000), timeframe="1m", repository=repo)
    sim = PaperSimulator(broker)
    attach(sim)
    for index, price in enumerate(PRICES[:5]):
        sim.process_bar(live_candle(index, price))
    buys_before = [o for o in broker.get_orders() if o["side"] == "BUY"]
    assert len(buys_before) == 1  # momentum BUY exactly once on the rising feed
    equity_before = broker.get_balance()["equity"]
    last_bar = broker.last_bars[SYMBOL]
    broker.close()
    db.close()

    db2 = Database(url)
    repo2 = Repository(db2)
    broker2 = PaperBroker(live_config(initial_capital=1000), timeframe="1m", repository=repo2)
    sim2 = PaperSimulator(broker2)
    attach(sim2)
    sim2.process_bar(live_candle(4, PRICES[4]))  # duplicate of the last pre-restart bar
    assert broker2.last_bars[SYMBOL] == last_bar
    assert [o["id"] for o in broker2.get_orders()] == [o["id"] for o in broker.get_orders()]
    assert broker2.get_balance()["equity"] == equity_before
    with pytest.raises(ValueError, match="Out-of-order"):
        sim2.process_bar(live_candle(2, PRICES[2]))  # stale replay older than the cursor
    sim2.process_bar(live_candle(5, PRICES[5]))  # next candle: rising market → HOLD
    ids = [o["id"] for o in broker2.get_orders()]
    assert len(ids) == len(set(ids)) == 1  # still exactly one, non-duplicated order
    assert not [o for o in broker2.get_orders() if o["side"] == "SELL"]
    assert broker2.get_positions()  # paper position survives restart and new bars
    broker2.close()
    db2.close()


def test_conflicting_duplicate_and_future_candle_fail_closed():
    async def scenario():
        broker = PaperBroker(live_config(), timeframe="1m")
        sim = PaperSimulator(broker)
        sim.provider = UpstoxStream(TOKEN, [KEY], timeframe="1m")
        sim.strategy = create_strategy("momentum", {"period": 2, "threshold": 0.0})
        sim.symbols, sim.state = [SYMBOL], BotState.RUNNING
        sim.process_bar(live_candle(0, 100.0))
        with pytest.raises(ValueError, match="unfinished/future"):
            future = live_candle(1, 101.0)
            future = Candle(
                datetime.now(timezone.utc).replace(microsecond=0),
                101.0, 101.5, 100.5, 101.2, 10_000, SYMBOL,
            )
            sim.process_bar(future)  # live provider may not send unclosed candles
        sim.process_bar(live_candle(1, 101.0))
        with pytest.raises(ValueError, match="Conflicting duplicate"):
            sim.process_bar(live_candle(1, 999.0))  # same timestamp, different data
        with pytest.raises(ValueError, match="Out-of-order"):
            sim.process_bar(live_candle(0, 100.0))

    asyncio.run(scenario())


def test_real_broker_remains_non_operational():
    # The safety stub raises whether via its guard or ABC abstract methods.
    with pytest.raises((RuntimeError, TypeError)):
        FutureRealBroker()


def test_health_keeps_legacy_paper_process_and_names_guarded_vantage_capability(tmp_path):
    settings = replace(
        Settings(),
        api_token="local-test-token",
        database_url=f"sqlite:///{tmp_path}/health.db",
        default_timeframe="1m",
    )
    with TestClient(create_app(settings, data_dir=tmp_path)) as client:
        health = client.get("/health").json()
        assert health["version"] == __version__
        assert health["mode"] == "PAPER" and health["live_trading"] is False
        assert health["real_order_routing"] == "VANTAGE_MT5_GUARDED"
        assert health["vantage_startup"] == "DISCONNECTED_DISARMED"
        headers = {"Authorization": "Bearer local-test-token"}
        status = client.get("/market/status", headers=headers).json()
        assert status["paper_mode"] is True and status["mode"] == "PAPER"
        assert status["live_trading"] is False
        assert status["state"] == "STOPPED"


def test_upstox_start_fails_closed_without_credentials(tmp_path):
    settings = replace(
        Settings(),
        api_token="local-test-token",
        database_url=f"sqlite:///{tmp_path}/nocred.db",
        data_source="upstox",
        default_timeframe="1m",
        market_data_timeframe="1m",
        market_data_symbols=KEY,
        market_data_access_token="",
    )
    with TestClient(create_app(settings, data_dir=tmp_path)) as client:
        headers = {"Authorization": "Bearer local-test-token"}
        response = client.post(
            "/paper/start",
            headers=headers,
            json={"strategy": "momentum", "symbol": SYMBOL, "timeframe": "1m", "source": "upstox"},
        )
        assert response.status_code == 422
        assert "access token" in response.json()["detail"]
        assert AUTHORIZE_URL.startswith("https://api.upstox.com/v3/")
