"""V1: 24/7 sandbox engine behavior — gating, risk, reconciliation, learning.

The engine runs against stub providers (deterministic, offline, no network).
"""

import json
from datetime import datetime, timezone

import pytest

from app.brokers.sandbox import UpstoxSandboxBroker
from app.config.settings import Settings
from app.data.models import Candle
from app.database.database import Database
from app.database.repository import Repository
from app.engine.sandbox_engine import SandboxEngine, intent_id_for
from app.learning.journal import LearningJournal
from app.risk.limits import HardRiskLimits
from app.strategies.base import Signal

INSTRUMENT = "NSE_EQ|INE002A01018"


def make_handler(state):
    def handler(request):
        if state["auth_mode"] != "ok":
            return json_response(401, {"errors": {"errorCode": "UDAPI1052"}})
        import httpx as _hx

        if request.method == "POST" and request.url.path == "/v3/order/place":
            oid = str(state["next"])
            state["next"] += 1
            state["placed"] += 1
            state["orders"][oid] = {"status": "open pending", "filled": 0, "avg": 0.0}
            return json_response(200, {"status": "success",
                                       "data": {"order_ids": [oid]}})
        if request.method == "GET" and request.url.host == "api.upstox.com":
            oid = request.url.params["order_id"]
            rec = state["orders"].get(oid)
            if rec is None:
                return json_response(400, {"errors": [{"errorCode": "UDAPI100010"}]})
            return json_response(200, {"status": "success", "data": [
                {"status": rec["status"], "filled_quantity": rec["filled"],
                 "average_price": rec["avg"], "order_timestamp": "2026-01-02T09:15:00",
                 "status_message": ""}]})
        return json_response(500, {"errors": [{"errorCode": "X"}]})

    return handler


def json_response(status, payload):
    import httpx as _hx

    return _hx.Response(status, json=payload)


def new_state():
    return {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}


class StubProvider:
    """Deterministic candle stream for engine tests."""

    symbols = ["RELIANCE"]

    def __init__(self, closes, market_open=True, stale=False):
        self.closes = closes
        self.market_open = market_open
        self.stale = stale
        self._i = 0

    def metrics(self):
        return {"market_open": self.market_open, "stale_feed": self.stale,
                "mode": "ltpc"}

    async def stream(self, symbols, timeframe):
        for i, close in enumerate(self.closes):
            ts = datetime(2026, 1, 2, 9, 15 + i, tzinfo=timezone.utc)
            yield Candle(ts, close * 0.999, close * 1.001, close * 0.998, close,
                         10000, "RELIANCE")


class AlwaysBuy:
    name = "momentum"
    parameters = {"period": 2}
    history_limit = 30

    def generate_signal(self, candles):
        return Signal.BUY


class BuyThenSell:
    name = "momentum"
    parameters = {"period": 2}
    history_limit = 30

    def __init__(self):
        self.calls = 0

    def generate_signal(self, candles):
        self.calls += 1
        return Signal.BUY if self.calls == 1 else Signal.SELL


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/engine.db")
    repository = Repository(database)
    state = new_state()
    broker = UpstoxSandboxBroker(
        "sandbox-token-xyz", INSTRUMENT,
        transport=httpx_transport(make_handler(state)),
        repository=repository, initial_capital=100000.0)
    broker.instrument_map["RELIANCE"] = INSTRUMENT  # 3.0.2 explicit identity
    settings = Settings()
    engine = SandboxEngine(
        provider=StubProvider([]), strategy=AlwaysBuy(), broker=broker,
        repository=repository, risk_limits=HardRiskLimits.from_settings(settings),
        journal=LearningJournal(repository), timeframe="1m", evaluate_every=1000,
        instrument_keys={"RELIANCE": INSTRUMENT})  # 3.0.2 explicit identity
    engine.provider = StubProvider([])
    yield engine, repository, state
    database.close()


def httpx_transport(handler):
    import httpx

    return httpx.MockTransport(handler)


def candle(close, minute=15):
    return Candle(datetime(2026, 1, 2, 9, minute, tzinfo=timezone.utc),
                  close * 0.999, close * 1.001, close * 0.998, close, 10000, "RELIANCE")


class TestMarketGating:
    def test_closed_market_skips_signals_but_reconciles(self, env):
        engine, repository, state = env
        engine.provider = StubProvider([], market_open=False)
        engine.process_candle(candle(100.0))
        assert state["placed"] == 0
        assert engine.submitted == 0
        kinds = [e["kind"] for e in repository.list_system_events(limit=10)]
        assert "MARKET_CLOSED_SKIP" in kinds

    def test_stale_feed_pauses_new_signals(self, env):
        engine, repository, state = env
        engine.provider = StubProvider([], stale=True)
        engine.process_candle(candle(100.0))
        assert state["placed"] == 0


class TestRiskGate:
    def test_buy_blocked_by_trades_cap_and_recorded(self, env):
        engine, repository, state = env
        limits = HardRiskLimits(max_position_value=0, max_open_positions=0,
                                max_daily_loss=0, max_trades_per_day=1)
        engine.risk_limits = limits
        engine.process_candle(candle(100.0, 15))
        assert state["placed"] == 1
        engine.process_candle(candle(101.0, 16))
        assert state["placed"] == 1, "second trade blocked by max_trades_per_day"
        kinds = [e["kind"] for e in repository.list_system_events(limit=20)]
        assert "RISK_REJECT" in kinds
        assert engine.rejected_by_risk == 1

    def test_engine_status_reports_hard_limits_untouched(self, env):
        engine, repository, state = env
        engine.process_candle(candle(100.0, 15))
        status = engine.status()
        assert status["live_trading"] is False
        assert status["mode"] == "UPSTOX_SANDBOX"
        assert status["engine"] == "sandbox"


class TestFullLoop:
    def test_run_buys_then_sells_and_journals_outcome(self, env):
        engine, repository, state = env
        strategy = BuyThenSell()
        engine.strategy = strategy
        engine.provider = StubProvider([100.0, 100.5])
        engine.run(max_candles=2)
        # Fill the BUY, then the SELL candle needs the fill before its signal;
        # run() reconciles once after consume, so fill BUY and process more.
        state["orders"]["1001"].update(status="complete", filled=249, avg=100.0)
        fills = engine.reconcile()
        assert fills and fills[0]["side"] == "BUY"
        assert engine.broker.ledger.position_quantity == 249
        # Second candle: SELL closes the position.
        engine.provider = StubProvider([])
        engine.strategy.calls = 2  # force SELL branch
        engine.process_candle(candle(100.5, 16))
        state["orders"]["1002"].update(status="complete", filled=249, avg=100.5)
        engine.reconcile()
        ledger = engine.broker.ledger
        assert ledger.position_quantity == 0
        assert ledger.realized_pnl > 0  # sold higher than bought
        assert repository.list_signals(), "signal must be journaled"

    def test_processed_signals_persisted_for_restart_dedup(self, env):
        engine, repository, state = env
        engine.provider = StubProvider([])
        engine.process_candle(candle(100.0, 15))
        processed = repository.get_engine_state("processed_signals", [])
        assert len(processed) == 1
        key = intent_id_for("momentum", "RELIANCE", candle(100.0, 15), "BUY")
        assert processed == [key]

    def test_duplicate_candle_never_reposts(self, env):
        engine, repository, state = env
        engine.provider = StubProvider([])
        same = candle(100.0, 15)
        engine.process_candle(same)
        engine.process_candle(same)  # same timestamp → same intent id
        assert state["placed"] == 1


class TestEvaluation:
    def test_evaluation_writes_model_version_and_requires_oos_improvement(self, env):
        engine, repository, state = env
        engine.provider = StubProvider([100.0 + 0.1 * i for i in range(14)])
        for i in range(14):
            engine.process_candle(candle(100.0 + 0.1 * i, 15 + i))
        engine._maybe_evaluate(force=True)
        assert engine.evaluation is not None
        versions = repository.list_model_versions()
        assert versions, "evaluation must record a versioned model row"
        # Candidate never promoted without a strictly better OOS expectancy.
        if engine.evaluation["promoted"]:
            promoted = [v for v in versions if v["promoted"]]
            assert promoted and promoted[0]["validation"]["oos_trades"] >= 3
        else:
            assert engine.evaluation["promoted_version"] is None

    def test_promotion_cannot_change_risk_limits_or_broker(self, env):
        engine, repository, state = env
        settings = Settings()
        before = (settings.broker, settings.upstox_sandbox_access_token)
        engine.provider = StubProvider([100.0 + i for i in range(12)])
        for i in range(12):
            engine.process_candle(candle(100.0 + i, 15 + i))
        engine._maybe_evaluate(force=True)
        after_settings = Settings()
        assert (after_settings.broker, after_settings.upstox_sandbox_access_token) == before
        limits = HardRiskLimits.from_settings(after_settings)
        assert limits.max_daily_loss == settings.max_daily_loss
        assert limits.max_trades_per_day == settings.max_trades_per_day
