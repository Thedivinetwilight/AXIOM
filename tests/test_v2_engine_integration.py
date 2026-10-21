"""V2.1: engine integration — regime, shadow, batched learning, snapshots."""

import httpx
import pytest

from app.brokers.sandbox import UpstoxSandboxBroker
from app.config.settings import Settings
from app.data.models import Candle
from app.database.database import Database
from app.database.repository import Repository
from app.engine.sandbox_engine import SandboxEngine
from app.learning.journal import LearningJournal
from app.learning.regime_engine import RegimeEngine
from app.learning.shadow import ShadowStrategyEngine
from app.risk.limits import HardRiskLimits
from app.strategies import create_strategy
from app.strategies.base import Signal
from tests.support import PassingNetEdgeGate

INSTRUMENT = "NSE_EQ|INE002A01018"


def make_handler(state):
    def handler(request):
        if request.method == "POST":
            oid = str(state["next"])
            state["next"] += 1
            state["placed"] += 1
            state["orders"][oid] = {"status": "open pending", "filled": 0, "avg": 0.0}
            return httpx.Response(200, json={"status": "success",
                                             "data": {"order_ids": [oid]}})
        rec = state["orders"].get(request.url.params["order_id"])
        if rec is None:
            return httpx.Response(400, json={"errors": [{"errorCode": "UDAPI100010"}]})
        return httpx.Response(200, json={"status": "success", "data": [
            {"status": rec["status"], "filled_quantity": rec["filled"],
             "average_price": rec["avg"], "order_timestamp": "t",
             "status_message": ""}]})
    return handler


import json  # noqa: E402


class AlwaysBuy:
    name = "momentum"
    parameters = {"period": 2}
    history_limit = 30

    def generate_signal(self, candles):
        return Signal.BUY


class _P:
    symbols = ["RELIANCE"]

    def metrics(self):
        return {"market_open": True, "stale_feed": False}


@pytest.fixture()
def engine_env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/eng.db")
    repository = Repository(database)
    state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
    broker = UpstoxSandboxBroker(
        "tok", INSTRUMENT, transport=httpx.MockTransport(make_handler(state)),
        repository=repository, initial_capital=100000.0)
    regime = RegimeEngine(repository)
    shadow = ShadowStrategyEngine(repository, regime_engine=regime)
    shadow.register(create_strategy("rsi", {"period": 14}), {"period": 14},
                    "minutes", 1)
    engine = SandboxEngine(
        provider=_P(), strategy=AlwaysBuy(), broker=broker, repository=repository,
        risk_limits=HardRiskLimits.from_settings(Settings()),
        journal=LearningJournal(repository), timeframe="1m", evaluate_every=5,
        instrument_keys={"RELIANCE": INSTRUMENT},  # 3.0.2 explicit identity
        regime_engine=regime, shadow_engine=shadow,
        net_edge_gate=PassingNetEdgeGate())
    yield engine, repository, state
    database.close()


def candle(minute, close=100.5):
    from datetime import datetime, timezone

    return Candle(datetime(2026, 1, 2, 9, minute, tzinfo=timezone.utc),
                  100, 101, 99, close, 10000, "RELIANCE")


class TestEngineV2:
    def test_regime_and_shadow_run_with_the_engine(self, engine_env):
        engine, repository, state = engine_env
        for minute in range(15, 21):
            engine.process_candle(candle(minute))
        assert engine.regime_engine.current is not None
        assert repository.list_shadow_trades(), "shadow candidates follow candles"
        kinds = [e["kind"] for e in repository.list_learning_events(limit=10)]
        assert "REGIME_CHANGE" in kinds or engine.regime_engine.current

    def test_learning_runs_in_batches_not_after_every_trade(self, engine_env):
        engine, repository, state = engine_env
        for minute in range(15, 24):  # 9 candles: below batch minimum
            engine.process_candle(candle(minute))
        assert [e for e in repository.list_learning_events(limit=10)
                if e["kind"] == "EVALUATION_RUN"] == []
        engine.process_candle(candle(24))  # 10th candle → first full batch
        assert any(e["kind"] == "EVALUATION_RUN"
                   for e in repository.list_learning_events(limit=10))

    def test_daily_snapshot_persisted(self, engine_env):
        engine, repository, state = engine_env
        engine.process_candle(candle(15))
        snapshots = repository.list_daily_snapshots()
        assert snapshots and snapshots[0]["date"] == "2026-01-02"
        assert snapshots[0]["regime"]

    def test_risk_gate_still_precedes_orders(self, engine_env):
        engine, repository, state = engine_env
        engine.risk_limits = HardRiskLimits(max_position_value=0, max_open_positions=0,
                                            max_daily_loss=0, max_trades_per_day=0)
        engine.process_candle(candle(15))
        assert state["placed"] == 1, "risk-allowed BUY reaches sandbox"
        engine.risk_limits = HardRiskLimits(max_position_value=0, max_open_positions=0,
                                            max_daily_loss=0, max_trades_per_day=1)
        engine.process_candle(candle(16))
        assert state["placed"] == 1, "second BUY blocked by the hard gate"
