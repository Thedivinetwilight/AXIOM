import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import pytest
from app.data.realtime import JSONWebSocketStream, ReplayStream
from app.data.historical import BinanceHistorical
from app.data.cache import DataCache
from app.data.models import parse_timestamp
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import PaperSimulator
from app.strategies import create_strategy
from app.database.database import Database
from app.database.repository import Repository
from app.backtesting.orders import Order
from app.backtesting.metrics import calculate
from tests.conftest import candle


def test_websocket_reconnect_auth_heartbeat(monkeypatch, caplog):
    import websockets.asyncio.client
    import app.data.realtime as realtime

    provider = JSONWebSocketStream("wss://example.com/feed", "private-test-token")
    attempts, subscriptions = [], []
    payload = {"closed": True, "timeframe": "1D", "candle": candle().to_dict()}

    class Socket:
        received = 0
        pings = 0

        async def send(self, value):
            subscriptions.append(json.loads(value))

        async def recv(self):
            self.received += 1
            if self.received == 1:
                raise TimeoutError()
            return json.dumps(payload)

        async def ping(self):
            self.pings += 1
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future

        async def close(self):
            pass

    socket = Socket()

    class Connection:
        def __init__(self, index):
            self.index = index

        async def __aenter__(self):
            if self.index == 1:
                raise OSError("network temporarily unavailable")
            return socket

        async def __aexit__(self, *args):
            pass

    def connect(url, **kwargs):
        attempts.append(kwargs)
        return Connection(len(attempts))

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    monkeypatch.setattr(realtime.asyncio, "sleep", no_wait)

    async def scenario():
        stream = provider.stream(["DEMO"], "1D")
        bar = await anext(stream)
        assert bar.close == 100
        await provider.close()
        await stream.aclose()

    asyncio.run(scenario())
    assert len(attempts) == 2 and socket.pings == 1
    assert attempts[-1]["additional_headers"]["Authorization"] == "Bearer private-test-token"
    assert attempts[-1]["ping_timeout"] == 20
    assert subscriptions == [{"type": "subscribe", "symbols": ["DEMO"], "timeframe": "1D"}]
    assert provider.status == "DISCONNECTED"
    assert "private-test-token" not in caplog.text


def test_historical_download_and_cache(monkeypatch, tmp_path):
    import httpx

    calls = []
    start = parse_timestamp("2020-01-01T00:00:00Z")
    end = start + timedelta(days=2)
    rows = [
        [
            int((start + timedelta(days=i)).timestamp() * 1000),
            "100",
            "102",
            "99",
            "101",
            "5000",
            int((start + timedelta(days=i + 1)).timestamp() * 1000) - 1,
        ]
        for i in range(2)
    ]

    def get(self, url, params):
        calls.append((url, params))
        return httpx.Response(200, json=rows, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr("app.data.historical.time.sleep", lambda _: None)
    provider = BinanceHistorical(DataCache(tmp_path / "cache"))
    downloaded = provider.historical("BTCUSDT", "1D", start, end)
    assert len(downloaded) == 2
    assert provider.historical("BTCUSDT", "1D", start, end) == downloaded
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/v3/klines")
    assert calls[0][1]["interval"] == "1d"


def test_paper_resume_idempotent(tmp_path, zero_cost, bars):
    db = Database("sqlite:///" + str(tmp_path / "resume.db"))
    repo = Repository(db)

    async def scenario():
        first = PaperBroker(zero_cost, repository=repo)
        sim = PaperSimulator(first)
        await sim.start(ReplayStream(bars[:100]), create_strategy("momentum"), ["DEMO"])
        await sim.task
        first.close()
        restored = PaperBroker(zero_cost, repository=repo)
        sim2 = PaperSimulator(restored)
        await sim2.start(ReplayStream(bars), create_strategy("momentum"), ["DEMO"])
        await sim2.task
        assert sim2.error is None
        assert sim2.processed == 200
        assert len({(f.order_id, f.timestamp) for f in restored.fills}) == len(restored.fills)
        restored.close()

    asyncio.run(scenario())
    db.close()


def test_transaction_rollback(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "rollback.db"))
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.execute("INSERT INTO state VALUES ('probe','{}')")
            raise RuntimeError("simulated interruption")
    with db.transaction() as connection:
        assert connection.execute("SELECT * FROM state WHERE key='probe'").fetchone() is None
    db.close()


def test_cost_decomposition(zero_cost):
    config = replace(zero_cost, commission=0.005, slippage=0.01, spread=0.02)
    broker = PaperBroker(config)
    broker.place_order(Order("DEMO", "BUY", 1, candle().timestamp.isoformat()))
    broker.on_bar(candle())
    snapshot = broker.get_balance()
    assert snapshot["spread_slippage_cost"] == pytest.approx(2)
    assert snapshot["fees"] == pytest.approx(0.51)
    assert snapshot["equity"] == pytest.approx(997.49)
    metrics = calculate([snapshot], [], 1000)
    assert metrics["transaction_costs"] == pytest.approx(2.51)


def test_aggregate_trade_metrics():
    snapshots = [
        {"timestamp": "2024-01-01T00:00:00+00:00", "equity": 1010, "exposed": True, "fees": 3},
        {"timestamp": "2024-01-02T00:00:00+00:00", "equity": 1005, "exposed": False, "fees": 4},
    ]
    metrics = calculate(snapshots, [{"pnl": 10}, {"pnl": -5}], 1000)
    assert metrics["profit_factor"] == 2
    assert metrics["expectancy"] == 2.5
    assert metrics["win_rate"] == 0.5
    assert metrics["average_win"] == metrics["largest_win"] == 10
    assert metrics["average_loss"] == metrics["largest_loss"] == -5
    assert metrics["max_drawdown"] == pytest.approx(1 - 1005 / 1010)


def test_conflicting_duplicate_fails(zero_cost):
    broker = PaperBroker(zero_cost)
    broker.on_bar(candle())
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        broker.on_bar(candle(price=101))


def test_live_future_candle_fails(zero_cost):
    sim = PaperSimulator(PaperBroker(zero_cost))
    sim.provider = JSONWebSocketStream("wss://example.com/data")
    future = replace(candle(), timestamp=datetime.now(timezone.utc) + timedelta(days=1))
    with pytest.raises(ValueError, match="unfinished/future"):
        sim.process_bar(future)
