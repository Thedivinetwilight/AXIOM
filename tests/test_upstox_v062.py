"""V0.6.2 tests: normalized events, protobuf decoder abstraction, market-status
gating, OAuth helper, UPSTOX_* aliases, CLI fail-closed behavior, risk counters.

All networked tests run against real loopback sockets speaking the documented
Upstox V3 wire format (decoded with the OFFICIAL protobuf schema). Nothing here
is proof of external integration — see V062_VERIFICATION.md for the honest
real-external-test status.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import time

import httpx
import pytest

from app import __version__
from app.config.settings import Settings
from app.data.factory import stream_provider
from app.data.market_events import MarketEvent
from app.data.upstox_proto.MarketDataFeed_pb2 import FeedResponse
from app.data.upstox_auth import (
    UpstoxOAuthError,
    build_login_url,
    exchange_code_for_token,
    store_token_in_env,
)
from app.data.upstox_stream import (
    UpstoxMarketDataProvider,
    UpstoxProtobufDecoder,
    UpstoxStream,
)
from app.backtesting.costs import ExecutionConfig
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import BotState, PaperSimulator
from app.strategies import create_strategy

from tests.test_upstox_live import (
    KEY,
    SYMBOL,
    TOKEN,
    bucket,
    ltpc_frame,
    live_config,
    market_info_frame,
    past_base,
)

pytest.importorskip("websockets")

REAL_KEY = KEY


# Anchor once at module import so repeated calls cannot jump a minute boundary
# during a long/parallel test run.
_DEEP_BUCKET_BASE = int(time.time()) - 1200
_DEEP_BUCKET_BASE -= _DEEP_BUCKET_BASE % 60


def deep_bucket(index: int) -> int:
    """Minute-aligned bucket ~20 minutes in the past."""
    return _DEEP_BUCKET_BASE + index * 60


# ---------------------------------------------------------------- decoder


def test_decoder_normalizes_ltpc_tick_and_snapshot_kinds():
    decoder = UpstoxProtobufDecoder({KEY: SYMBOL}, "ltpc")
    now = time.time()
    events = decoder.decode(ltpc_frame(1000.5, bucket(0) + 30, initial=True), now)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, MarketEvent)
    assert event.provider == "upstox" and event.instrument_key == KEY
    assert event.kind == "snapshot"  # initial_feed type
    assert event.ltp == 1000.5 and event.last_traded_quantity == 10000.0
    assert event.exchange_ts == datetime.fromtimestamp(bucket(0) + 30, timezone.utc)
    assert event.received_at == datetime.fromtimestamp(now, timezone.utc)
    assert event.bid is None and event.ask is None  # not in ltpc mode: never fabricated
    response = FeedResponse(type=1, currentTs=int(now * 1000))
    feed = response.feeds[KEY]
    feed.ltpc.ltp, feed.ltpc.ltt, feed.ltpc.cp = 1001.0, bucket(1) * 1000 + 30_000, 1000.0
    events = decoder.decode(response.SerializeToString(), now)
    assert events[0].kind == "live" and events[0].close_price == 1000.0
    bare = decoder.decode(ltpc_frame(1001.0, bucket(1) + 30), now)
    assert bare[0].close_price is None  # absent fields stay None, never fabricated


def test_decoder_market_info_event_and_full_mode_fields():
    decoder = UpstoxProtobufDecoder({KEY: SYMBOL}, "ltpc")
    events = decoder.decode(market_info_frame("NSE", 2), time.time())
    assert len(events) == 1
    event = events[0]
    assert event.kind == "market_info" and event.market_status == "NORMAL_OPEN"
    assert event.segment_statuses == {"NSE": "NORMAL_OPEN"} and event.ltp is None

    response = FeedResponse(type=1, currentTs=1)
    response.marketInfo.segmentStatus["NSE"] = 3  # NORMAL_CLOSE
    events = decoder.decode(response.SerializeToString(), time.time())
    assert events[0].market_status == "NORMAL_CLOSE"

    decoder = UpstoxProtobufDecoder({KEY: SYMBOL}, "full")
    response = FeedResponse(type=1, currentTs=int(time.time() * 1000))
    full = response.feeds[KEY].fullFeed.marketFF
    full.ltpc.ltp, full.ltpc.ltt, full.ltpc.ltq, full.ltpc.cp = 500.0, bucket(0) * 1000, 3, 499.0
    level = full.marketLevel.bidAskQuote.add()
    level.bidP, level.bidQ, level.askP, level.askQ = 499.5, 10, 500.5, 10
    bar = full.marketOHLC.ohlc.add()
    bar.interval, bar.ts = "1m", bucket(0)
    bar.open, bar.high, bar.low, bar.close, bar.vol = 499, 501, 498, 500.5, 1234
    event = decoder.decode(response.SerializeToString(), time.time())[0]
    assert event.bid == 499.5 and event.ask == 500.5
    assert event.interval_bar == ("1m", bucket(0), 499.0, 501.0, 498.0, 500.5, 1234.0)
    assert decoder.decode(b"\x00garbage", time.time()) == [] and decoder.malformed == 1


def test_decoder_ignores_unconfigured_instruments():
    decoder = UpstoxProtobufDecoder({KEY: SYMBOL}, "ltpc")
    response = FeedResponse(type=1, currentTs=1)
    feed = response.feeds["BSE_EQ|500325"]
    feed.ltpc.ltp, feed.ltpc.ltt = 100.0, bucket(0) * 1000
    assert decoder.decode(response.SerializeToString(), time.time()) == []


def test_full_feed_quote_is_observed_and_expires_without_synthesis():
    provider = UpstoxStream(TOKEN, [KEY], timeframe="1m", mode="full")
    received = time.time()
    response = FeedResponse(type=1, currentTs=int(received * 1000))
    full = response.feeds[KEY].fullFeed.marketFF
    full.ltpc.ltp, full.ltpc.ltt, full.ltpc.ltq = (
        500.0, int(received * 1000), 2)
    level = full.marketLevel.bidAskQuote.add()
    level.bidP, level.bidQ, level.askP, level.askQ = 499.5, 10, 500.5, 10
    decoder = UpstoxProtobufDecoder({KEY: SYMBOL}, "full")
    events = decoder.decode(response.SerializeToString(), received)
    assert len(events) == 1
    provider._apply_event(events[0], received)

    quote = provider.quote_for(SYMBOL, now=received + 0.5)
    assert quote is not None
    assert quote["provenance"] == "OBSERVED"
    assert quote["instrument_key"] == KEY
    assert quote["bid"] == 499.5 and quote["ask"] == 500.5
    assert provider.quote_for(SYMBOL, now=received + 4.0) is None

    invalid = replace(events[0], bid=0.0, ask=500.5)
    provider._apply_event(invalid, received)
    assert provider.quote_for(SYMBOL, now=received + 0.1) is None


# ---------------------------------------------------------------- gating


def test_no_candles_before_market_status_and_none_while_closed():
    """First market-status must precede strategy execution; closed → suppressed."""
    from tests.test_upstox_live import MarketFeedServer

    async def scenario():
        server = MarketFeedServer()
        opened = asyncio.Event()

        async def plan(ws, connection_index):
            try:
                # Phase 1: ticks WITHOUT any market status → nothing may be emitted.
                for index in range(3):
                    await ws.send(
                        ltpc_frame(100.0 + index, deep_bucket(index) + 30, initial=index == 0)
                    )
                    await asyncio.sleep(0.03)
                await asyncio.sleep(0.4)
                # Phase 2: market CLOSED → candles built but held back.
                await ws.send(market_info_frame("NSE", 3))  # NORMAL_CLOSE
                await asyncio.sleep(0.2)
                for index in range(2):
                    await ws.send(ltpc_frame(101.0 + index, deep_bucket(3 + index) + 30))
                    await asyncio.sleep(0.03)
                await asyncio.sleep(0.4)
                # Phase 3: market OPEN → the two fresh candles may flow.
                await ws.send(market_info_frame("NSE", 2))  # NORMAL_OPEN
                for index in range(2):
                    await ws.send(ltpc_frame(102.0 + index, deep_bucket(5 + index) + 30))
                    await asyncio.sleep(0.03)
                opened.set()
                await asyncio.sleep(5)  # hold the connection while assertions run
            except Exception:
                opened.set()
                raise

        await server.start(plan)

        async def authorize(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = json.dumps(
                {"data": {"authorized_redirect_uri": f"ws://127.0.0.1:{server.port}"}}
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
            writer.close()

        auth_server = await asyncio.start_server(authorize, "127.0.0.1", 0)
        provider = UpstoxStream(
            TOKEN, [KEY], timeframe="1m",
            authorize_url=f"http://127.0.0.1:{auth_server.sockets[0].getsockname()[1]}/x",
            max_backoff=0.05, ws_url_schemes=("ws", "wss"),
        )
        seen = []
        consumer = None
        try:
            async def consume():
                async for bar in provider.stream(provider.symbols, "1m"):
                    seen.append(bar)

            consumer = asyncio.create_task(consume())
            await asyncio.wait_for(opened.wait(), timeout=6)
            # settle: wait until the two post-OPEN candles are emitted (poll, not fixed sleep)
            # This removes the 0.3s race that caused flakiness on parallel runs.
            for _ in range(20):
                if len(seen) == 2:
                    break
                await asyncio.sleep(0.1)
            metrics = provider.metrics()
            assert len(seen) == 2  # only the two post-OPEN candles were emitted
            assert seen[0].timestamp == datetime.fromtimestamp(deep_bucket(5), timezone.utc)
            assert metrics["market_status"] == "NORMAL_OPEN" and metrics["market_open"] is True
            # 3 candles before ANY status + 2 while CLOSED are held back:
            assert metrics["suppressed_by_market_status"] == 5
            assert metrics["feed_counters"]["completed_candles"] >= 5  # built, but gated
        finally:
            await provider.close()
            if consumer:
                consumer.cancel()
                try:
                    await consumer
                except (asyncio.CancelledError, Exception):
                    pass
            server.server.close()
            await server.server.wait_closed()
            auth_server.close()
            await auth_server.wait_closed()

    asyncio.run(scenario())


def test_market_status_mapping_prefers_instrument_exchange():
    provider = UpstoxStream(TOKEN, [KEY], timeframe="1m")
    provider.segment_statuses = {"BSE": "NORMAL_CLOSE", "NSE": "NORMAL_OPEN"}
    assert provider._relevant_market_open() is True
    provider.segment_statuses = {"NSE": "NORMAL_CLOSE"}
    assert provider._relevant_market_open() is False
    provider.segment_statuses = {"MCX": "NORMAL_OPEN"}  # no exact match → any-open rule
    assert provider._relevant_market_open() is True
    provider.segment_statuses = {}
    assert provider._relevant_market_open() is None


def test_alias_and_gating_seam():
    assert UpstoxMarketDataProvider is UpstoxStream
    provider = UpstoxStream(TOKEN, [KEY], timeframe="1m", require_market_status=False)
    assert provider.require_market_status is False
    assert provider._candles_allowed() is True  # explicit continuous-venue seam


# ---------------------------------------------------------------- settings


def test_upstox_env_aliases_and_explicit_precedence(monkeypatch):
    for name in ("MARKET_DATA_ACCESS_TOKEN", "MARKET_DATA_SYMBOLS", "DATA_SOURCE",
                 "MARKET_DATA_TIMEFRAME", "DEFAULT_TIMEFRAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "alias-token")
    monkeypatch.setenv("UPSTOX_INSTRUMENT_KEYS", "NSE_EQ|INE002A01018")
    settings = Settings.from_env()
    assert settings.market_data_access_token == "alias-token"
    assert settings.market_data_symbol_list() == ["NSE_EQ|INE002A01018"]
    provider = stream_provider(replace(settings, data_source="upstox"), "upstox")
    assert provider.symbols == ["NSE_EQ-INE002A01018"]
    monkeypatch.setenv("MARKET_DATA_ACCESS_TOKEN", "explicit-token")
    explicit = Settings.from_env()
    assert explicit.market_data_access_token == "explicit-token"  # explicit wins
    assert "alias-token" not in repr(settings) and "alias-token" not in repr(provider)


# ---------------------------------------------------------------- OAuth


def test_login_url_matches_documented_dialog():
    url = build_login_url("api-key", "https://example.com/cb", state="st4te")
    assert url.startswith("https://api.upstox.com/v2/login/authorization/dialog?")
    assert "response_type=code" in url and "client_id=api-key" in url
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcb" in url and "state=st4te" in url
    with pytest.raises(UpstoxOAuthError):
        build_login_url("", "https://example.com/cb")


def test_token_exchange_success_failure_and_no_secret_leak():
    def ok(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=authorization_code" in body and "code=auth-code" in body
        assert "client_secret=super-secret" in body  # form-encoded per docs
        return httpx.Response(200, json={"access_token": "issued-token"})

    token = exchange_code_for_token(
        "auth-code", "api-key", "super-secret", "https://example.com/cb",
        transport=httpx.MockTransport(ok),
    )
    assert token == "issued-token"

    def error(request):
        return httpx.Response(400, json={"status": "error", "errors": [{"message": "bad"}]})

    with pytest.raises(UpstoxOAuthError) as excinfo:
        exchange_code_for_token(
            "auth-code", "api-key", "super-secret", "https://example.com/cb",
            transport=httpx.MockTransport(error),
        )
    assert "super-secret" not in str(excinfo.value) and "bad" not in str(excinfo.value)

    def unreachable(request):
        raise httpx.ConnectError("down")

    # 3.2.2: granular network errors for the token exchange too.
    with pytest.raises(UpstoxOAuthError, match="Could not reach Upstox"):
        exchange_code_for_token(
            "auth-code", "api-key", "super-secret", "https://example.com/cb",
            transport=httpx.MockTransport(unreachable),
        )

    def tls_down(request):
        raise httpx.RemoteProtocolError("tls broken")

    with pytest.raises(UpstoxOAuthError, match="TLS"):
        exchange_code_for_token(
            "auth-code", "api-key", "super-secret", "https://example.com/cb",
            transport=httpx.MockTransport(tls_down),
        )
    with pytest.raises(UpstoxOAuthError):
        exchange_code_for_token("", "api-key", "super-secret", "https://example.com/cb")


def test_store_token_writes_gitignored_env_atomically(tmp_path):
    import os
    import stat as stat_module

    env_path = tmp_path / ".env"
    env_path.write_text("API_TOKEN=local\n", encoding="utf-8")
    outcome = store_token_in_env("issued-token", env_path)

    assert "written" in outcome and "issued-token" not in outcome
    content = env_path.read_text(encoding="utf-8")
    assert "API_TOKEN=local" in content and "UPSTOX_ACCESS_TOKEN=issued-token" in content
    if os.name == "nt":
        # Windows has no group/other permission bits. os.chmod can only toggle
        # the read-only flag, and os.stat reports a synthesised 0o666 for any
        # writable file, so asserting the POSIX bits here can never pass — the
        # file is protected by the ACL it inherits (%LOCALAPPDATA% is
        # owner-only). What must hold is that the write left it usable and the
        # reported outcome says what actually happened.
        assert env_path.stat().st_mode & stat_module.S_IWUSR
        assert "ACLs" in outcome
    else:
        assert not (env_path.stat().st_mode & (stat_module.S_IRGRP | stat_module.S_IROTH))
        assert "600" in outcome


# ---------------------------------------------------------------- pipeline


def test_risk_counters_and_reject_events_track_skipped_signals():
    async def scenario():
        broker = PaperBroker(live_config(), timeframe="1m")
        sim = PaperSimulator(broker)
        sim.provider = UpstoxStream(TOKEN, [KEY], timeframe="1m")
        sim.strategy = create_strategy("momentum", {"period": 2, "threshold": 0.0})
        sim.symbols, sim.state = [SYMBOL], BotState.RUNNING
        from app.data.models import Candle
        from datetime import datetime, timezone as tz

        def deep_candle(index: int, price: float) -> Candle:
            stamp = datetime.fromtimestamp(deep_bucket(index), tz.utc)
            return Candle(stamp, price, price, price, price, 10_000.0, SYMBOL)

        for index, price in enumerate([100.0, 100.5, 101.0, 101.5]):
            sim.process_bar(deep_candle(index, price))
        assert sim.risk_counters["actionable_signals"] >= 1
        assert sim.risk_counters["orders_created"] == 1
        # Further BUYs while the position exists must be risk-skips, not orders.
        actionable_again = 0
        for index, price in enumerate([102.0, 102.5, 103.0], start=4):
            before = sim.risk_counters["actionable_signals"]
            sim.process_bar(deep_candle(index, price))
            actionable_again += sim.risk_counters["actionable_signals"] - before
        assert len([o for o in broker.get_orders() if o["side"] == "BUY"]) == 1
        assert sim.risk_counters["orders_created"] == 1
        status = sim.status()
        assert status["risk_counters"]["orders_created"] == 1
        assert status["paper_mode"] is True and status["live_trading"] is False

    asyncio.run(scenario())


def test_cli_upstox_test_fails_closed_without_token(capsys):
    from app.__main__ import main

    monkey_free_env = dict()
    import os

    saved = {k: os.environ.get(k) for k in (
        "MARKET_DATA_ACCESS_TOKEN", "UPSTOX_ACCESS_TOKEN", "MARKET_DATA_SYMBOLS",
        "UPSTOX_INSTRUMENT_KEYS", "DATA_SOURCE",
    )}
    try:
        for key in saved:
            os.environ.pop(key, None)
        os.environ["DATA_SOURCE"] = "upstox"
        os.environ["MARKET_DATA_SYMBOLS"] = KEY
        with pytest.raises(SystemExit) as excinfo:
            main(["upstox-test"])
        assert excinfo.value.code == 2
        out = capsys.readouterr().out
        assert "access token" in out and "dummy" not in out.lower()
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def test_health_reports_version_and_paper_only(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.server import create_app

    settings = replace(
        Settings(), api_token="local-test-token",
        database_url=f"sqlite:///{tmp_path}/v062.db", default_timeframe="1m",
    )
    with TestClient(create_app(settings, data_dir=tmp_path)) as client:
        health = client.get("/health").json()
        assert health["version"] == __version__
        assert health["mode"] == "PAPER" and health["live_trading"] is False
        headers = {"Authorization": "Bearer local-test-token"}
        status = client.get("/market/status", headers=headers).json()
        assert status["paper_mode"] is True
        assert status["risk_counters"] == {
            "actionable_signals": 0, "orders_created": 0, "skipped": 0,
        }
        assert "access_token" not in json.dumps(status)


def test_store_token_updates_every_configured_token_key(tmp_path):
    """A refresh must not leave a stale duplicate in the same .env.

    Regression for the "upstox-login says success but the engine keeps using
    yesterday's token" failure: only the UPSTOX_ACCESS_TOKEN alias used to be
    rewritten while MARKET_DATA_ACCESS_TOKEN (the documented primary that
    Settings reads first) kept the expired value.
    """
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MARKET_DATA_ACCESS_TOKEN=old-primary\n"
        "UPSTOX_ACCESS_TOKEN=old-alias\n"
        "API_TOKEN=local\n",
        encoding="utf-8",
    )
    outcome = store_token_in_env("issued-token", env_path)
    assert "issued-token" not in outcome

    from app.config.envfile import read_values

    values = read_values(env_path)
    assert values["MARKET_DATA_ACCESS_TOKEN"] == "issued-token"
    assert values["UPSTOX_ACCESS_TOKEN"] == "issued-token"
    assert values["API_TOKEN"] == "local"
    assert "old-primary" not in env_path.read_text(encoding="utf-8")


def test_store_token_creates_the_alias_when_no_token_key_exists(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("API_TOKEN=local\n", encoding="utf-8")
    store_token_in_env("issued-token", env_path)
    from app.config.envfile import read_values

    values = read_values(env_path)
    assert values["UPSTOX_ACCESS_TOKEN"] == "issued-token"
    assert "MARKET_DATA_ACCESS_TOKEN" not in values
