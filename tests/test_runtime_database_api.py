import asyncio
from dataclasses import replace
import importlib
import pkgutil
import pytest
from fastapi.testclient import TestClient
from app.api.server import create_app
from app.config.settings import Settings, ROOT
from app.database.database import Database
from app.database.repository import Repository
from app.data.realtime import ReplayStream, BinanceStream, JSONWebSocketStream
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import PaperSimulator, BotState
from app.strategies import create_strategy
from app.backtesting.engine import BacktestEngine
from app.backtesting.orders import Order
from tests.conftest import candle


def test_all_imports():
    import app

    for module in pkgutil.walk_packages(app.__path__, prefix="app."):
        importlib.import_module(module.name)


def test_safety_settings(monkeypatch):
    with pytest.raises(ValueError):
        Settings(live_trading=True)
    with pytest.raises(ValueError):
        Settings(mode="LIVE")
    monkeypatch.setenv("LIVE_TRADING", "true")
    with pytest.raises(ValueError):
        Settings.from_env()
    assert "secret" not in repr(Settings(api_token="secret", market_data_token="secret"))


def test_persistence_and_writer_lock(tmp_path, zero_cost):
    url = "sqlite:///" + str(tmp_path / "paper.db")
    db = Database(url)
    repo = Repository(db)
    broker = PaperBroker(zero_cost, repository=repo)
    broker.place_order(Order("DEMO", "BUY", 1, candle().timestamp.isoformat()))
    broker.on_bar(candle())
    other_db = Database(url)
    other_repo = Repository(other_db)
    with pytest.raises(RuntimeError, match="Another paper"):
        PaperBroker(zero_cost, repository=other_repo)
    broker.close()
    restored = PaperBroker(zero_cost, repository=other_repo)
    assert restored.get_positions()[0]["quantity"] == 1
    assert restored.get_balance()["equity"] == 1000
    assert restored.on_bar(candle()) is None
    restored.close()
    db.close()
    other_db.close()


def test_paper_matches_backtest(bars, zero_cost):
    expected = BacktestEngine(zero_cost).run(bars, create_strategy("momentum"))

    async def scenario():
        broker = PaperBroker(zero_cost)
        sim = PaperSimulator(broker)
        await sim.start(ReplayStream(bars), create_strategy("momentum"), ["DEMO"])
        await sim.task
        assert sim.state == BotState.STOPPED
        assert sim.processed == len(bars)
        assert broker.get_balance()["equity"] == pytest.approx(expected.equity_curve[-1]["equity"])
        assert len(broker.portfolio.trades) == len(expected.trades)

    asyncio.run(scenario())


def test_pause_resume_stop(zero_cost, bars):
    async def scenario():
        sim = PaperSimulator(PaperBroker(zero_cost))
        await sim.start(ReplayStream(bars, delay=0.001), create_strategy("momentum"), ["DEMO"])
        await asyncio.sleep(0.01)
        await sim.pause()
        assert sim.state == BotState.PAUSED
        with pytest.raises(ValueError):
            await sim.start(ReplayStream(bars), create_strategy("momentum"), ["DEMO"])
        await sim.resume()
        assert sim.state == BotState.RUNNING
        await sim.stop()
        assert sim.task.done() and sim.state == BotState.STOPPED

    asyncio.run(scenario())


def test_live_gap_pauses_and_corrupt_bar_errors(zero_cost):
    async def scenario():
        sim = PaperSimulator(PaperBroker(zero_cost))
        await sim.start(
            ReplayStream([candle(), candle(2)], delay=0.001), create_strategy("momentum"), ["DEMO"]
        )
        await sim.task
        assert "Data gap" in sim.error
        assert not sim.broker.fills
        sim2 = PaperSimulator(PaperBroker(zero_cost))
        await sim2.start(
            ReplayStream([replace(candle(), volume=-1)]), create_strategy("momentum"), ["DEMO"]
        )
        await sim2.task
        assert sim2.state == BotState.ERROR
        assert sim2.error == "ValueError"

    asyncio.run(scenario())


def test_stream_decode_and_security():
    stream = BinanceStream()
    assert stream.decode({"result": None}, "1m") is None
    data = {
        "s": "BTCUSDT",
        "k": {
            "t": 1704067200000,
            "x": True,
            "i": "1m",
            "o": "100",
            "h": "101",
            "l": "99",
            "c": "100",
            "v": "10",
        },
    }
    assert stream.decode(data, "1m").symbol == "BTCUSDT"
    with pytest.raises(ValueError):
        stream.subscription(["../bad"], "1m")
    with pytest.raises(ValueError):
        JSONWebSocketStream("ws://insecure.example")
    with pytest.raises(ValueError):
        JSONWebSocketStream("wss://secret:password@example.com")
    generic = JSONWebSocketStream("wss://example.com/feed", "secret")
    assert generic.decode({"closed": False}, "1D") is None
    assert (
        generic.decode(
            {"closed": True, "timeframe": "1D", "candle": candle().to_dict()}, "1D"
        ).close
        == 100
    )


def test_api(tmp_path):
    settings = Settings(
        api_token="test-token", database_url="sqlite:///" + str(tmp_path / "api.db")
    )
    application = create_app(settings, ROOT / "data")
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(application) as client:
        assert client.get("/health").json()["live_trading"] is False
        assert client.get("/").status_code == 200
        assert client.get("/paper/account").status_code == 401
        for endpoint in [
            "/market/status",
            "/market/datasets",
            "/strategies",
            "/backtests",
            "/paper/account",
            "/paper/positions",
            "/paper/orders",
            "/paper/trades",
            "/paper/status",
            "/tournaments",
        ]:
            response = client.get(endpoint, headers=headers)
            assert response.status_code == 200, response.text
            assert "test-token" not in response.text
        bad = client.post("/backtests/run", headers=headers, json={"dataset": "../secret.csv"})
        assert bad.status_code == 422
        bad = client.post("/backtests/run", headers=headers, json={"strategy": "unknown"})
        assert bad.status_code == 422
        bad = client.post("/backtests/run", headers=headers, json={"live_trading": True})
        assert bad.status_code == 422
        response = client.post(
            "/backtests/run",
            headers=headers,
            json={"strategy": "momentum", "parameters": {"period": 10}},
        )
        assert response.status_code == 200, response.text
        backtest_id = response.json()["id"]
        assert (
            client.get(f"/backtests/{backtest_id}", headers=headers).json()["label"] == "BACKTEST"
        )
        assert client.get("/backtests/missing", headers=headers).status_code == 404
        response = client.post(
            "/paper/start", headers=headers, json={"source": "replay", "replay_delay": 0.01}
        )
        assert response.status_code == 200, response.text
        assert client.post("/paper/pause", headers=headers).status_code == 200
        assert client.post("/paper/resume", headers=headers).status_code == 200
        assert client.post("/paper/stop", headers=headers).json()["state"] == "STOPPED"
        assert client.post("/paper/stop", headers=headers).status_code == 200


def test_api_no_token_disables_control(tmp_path):
    settings = Settings(database_url="sqlite:///" + str(tmp_path / "api.db"))
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/paper/start", json={}).status_code == 503
