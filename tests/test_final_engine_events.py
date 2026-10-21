"""FINAL: event triggers, PauseGate + engine execution skip, scanner quality.

Written against the ACTUAL APIs: EventDrivenDecisionEngine(move_threshold=…,
volume_spike=…).on_candle(candle, history) → Decisions; PauseGate(repo);
OpportunityScanner(repo, store, settings=…).scan(instrument_keys, strategies,
evidence=…) over CandleStore rows.
"""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.data.models import Candle
from app.database.database import Database
from app.database.repository import Repository
from app.engine.events import EventDrivenDecisionEngine, PauseGate
from app.history.store import CandleStore, parse_local
from app.research.scanner import OpportunityScanner
from app.strategies import REGISTRY
from tests.support import PassingNetEdgeGate

BASE = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)


def candle(i, price, volume=1000, symbol="X"):
    ts = BASE + timedelta(minutes=i)
    return Candle(ts, price * 0.999, price * 1.001, price * 0.998, price,
                  volume, symbol)


def as_rows(candles):
    return [{"ts": parse_local(c.timestamp).isoformat(), "open": c.open,
             "high": c.high, "low": c.low, "close": c.close,
             "volume": c.volume, "oi": 0} for c in candles]


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/events.db")
    repository = Repository(database)
    store = CandleStore(tmp_path / "store", parquet_enabled=False)
    yield repository, store
    database.close()


class TestEventTriggers:
    def test_cadence_and_move_and_volume(self, env):
        events = EventDrivenDecisionEngine(volume_spike=2.0)
        events.register_cadence("momentum", 5)
        rng = random.Random(1)
        price = 100.0
        history = []
        triggers = []
        for i in range(20):
            price *= 1 + rng.uniform(-0.001, 0.001)
            current = candle(i, price)
            for decision in events.on_candle(current, history):
                triggers.append(decision.trigger)
            history.append(current)
        assert "cadence" in triggers and triggers[-1] == "cadence"
        # significant move: +1% > 0.3% threshold
        moved = candle(21, price * 1.01)
        triggers += [d.trigger for d in events.on_candle(moved, history)]
        assert "significant_move" in triggers
        # volume spike: 2500 ≥ 2 × mean(last 5 = 1000)
        spiky = candle(22, price, volume=2500)
        triggers += [d.trigger for d in events.on_candle(spiky, history)]
        assert "volume_spike" in triggers

    def test_regime_change_via_regime_engine(self, env):
        events = EventDrivenDecisionEngine()

        class FakeRegime:
            current = None

        regime = FakeRegime()
        history = [candle(i, 100.0) for i in range(6)]
        assert events.on_candle(candle(7, 100.0), history, regime) == []
        regime.current = "range_low_vol"
        assert events.on_candle(candle(8, 100.0), history, regime) == []
        regime.current = "trend_up_normal"  # change fires exactly once
        first = [d.trigger for d in events.on_candle(candle(9, 100.0), history, regime)]
        again = [d.trigger for d in events.on_candle(candle(10, 100.0), history, regime)]
        assert "regime_change" in first and "regime_change" not in again

    def test_order_event_hook(self, env):
        events = EventDrivenDecisionEngine()
        decisions = events.on_order_event({"strategy": "momentum",
                                           "event": "SUBMITTED"})
        assert decisions and decisions[0].trigger == "order_event"
        assert decisions[0].strategy == "momentum"


class TestPauseGate:
    def test_pause_roundtrip_persisted(self, env):
        repository, _ = env
        gate = PauseGate(repository)
        assert not gate.is_paused("execution")
        gate.pause("execution", "anomaly: order frequency spike")
        assert gate.is_paused("execution")
        flag = repository.get_engine_state("paused:execution")
        assert flag["paused"] is True and "spike" in flag["reason"]
        gate.resume("execution")
        assert not gate.is_paused("execution")
        kinds = {row["kind"] for row in repository.list_system_events(limit=10)}
        assert {"COMPONENT_PAUSED", "COMPONENT_RESUMED"} <= kinds

    def test_pause_survives_new_gate_instance(self, env):
        repository, _ = env
        PauseGate(repository).pause("research", "test")
        assert PauseGate(repository).is_paused("research")


class TestEnginePauseSkip:
    def test_paused_engine_records_skip_not_orders(self, tmp_path, env):
        import httpx

        from app.brokers.sandbox import UpstoxSandboxBroker
        from app.config.settings import Settings
        from app.engine.sandbox_engine import SandboxEngine
        from app.learning.journal import LearningJournal
        from app.risk.limits import HardRiskLimits

        repository, _ = env
        gate = PauseGate(repository)
        gate.pause("execution", "anomaly test")

        state = {"placed": 0, "orders": {}, "next": 1001}

        def handler(request):
            if request.method == "POST":
                state["placed"] += 1
                return httpx.Response(200, json={"status": "success",
                                                 "data": {"order_ids": ["1"]}})
            return httpx.Response(400, json={"errors": [{"errorCode": "UDAPI100010"}]})

        class AlwaysBuy:
            name = "momentum"
            parameters = {"period": 2}
            history_limit = 30

            def generate_signal(self, candles):
                from app.strategies.base import Signal

                return Signal.BUY

        class _P:
            symbols = ["RELIANCE"]

            def metrics(self):
                return {"market_open": True, "stale_feed": False}

        broker = UpstoxSandboxBroker(
            "tok", "NSE_EQ|INE002A01018",
            transport=httpx.MockTransport(handler), repository=repository,
            initial_capital=100000.0)
        broker.instrument_map["X"] = "NSE_EQ|INE002A01018"  # 3.0.2 explicit
        sandbox = SandboxEngine(
            provider=_P(), strategy=AlwaysBuy(), broker=broker,
            repository=repository, risk_limits=HardRiskLimits.from_settings(Settings()),
            instrument_keys={"X": "NSE_EQ|INE002A01018"},  # 3.0.2 explicit
            journal=LearningJournal(repository), timeframe="1m", evaluate_every=5,
            pause_gate=gate, net_edge_gate=PassingNetEdgeGate())
        for i in range(40):
            sandbox.process_candle(candle(i, 100.0 + i * 0.01))
        assert state["placed"] == 0, "paused execution must not place orders"
        skips = [e for e in repository.list_system_events(limit=50)
                 if e["kind"] == "EXECUTION_PAUSED_SKIP"]
        assert skips, "skip recorded (reason lives in paused:execution state)"
        assert skips[0]["payload"]["side"] == "BUY"
        paused = repository.get_engine_state("paused:execution")
        assert paused["reason"] == "anomaly test"


class TestScanner:
    def _store_series(self, store, key="NSE_EQ|AAA", n=60, growth=1.003,
                      volume=10000):
        price = 100.0
        candles = []
        symbol = key.split("|")[-1]
        for i in range(n):
            price *= growth
            candles.append(candle(i, price, volume=volume, symbol=symbol))
        store.upsert_candles(key, "days", 1, as_rows(candles))
        return candles

    def test_positive_quality_and_persisted_rows(self, env):
        repository, store = env
        self._store_series(store)
        scanner = OpportunityScanner(repository, store, settings=type(
            "S", (), {"commission": 0.0005, "slippage": 0.0002,
                      "spread": 0.0005, "scanner_top_k": 5})())
        evidence = {"momentum": {"oos_expectancy": 0.9, "oos_trades": 8}}
        opportunities = scanner.scan(["NSE_EQ|AAA"], [REGISTRY["momentum"]()],
                                    evidence=evidence)
        assert opportunities, "real edge with tiny costs must score positive"
        best = opportunities[0]
        assert best["score"] > 0 and best["action"] in {"BUY", "SELL"}
        rows = repository.list_opportunities(limit=5)
        assert rows and float(rows[0]["score"]) == pytest.approx(best["score"], abs=1e-9)

    def test_no_evidence_or_negative_edge_never_qualifies(self, env):
        repository, store = env
        self._store_series(store)
        scanner = OpportunityScanner(repository, store, settings=type(
            "S", (), {"commission": 0.0005, "slippage": 0.0002,
                      "spread": 0.0005, "scanner_top_k": 5})())
        assert scanner.scan(["NSE_EQ|AAA"], [REGISTRY["momentum"]()]) == []
        negative = {"momentum": {"oos_expectancy": -0.5, "oos_trades": 9}}
        assert scanner.scan(["NSE_EQ|AAA"], [REGISTRY["momentum"]()],
                            evidence=negative) == []

    def test_insufficient_history_skipped_silently(self, env):
        repository, store = env
        self._store_series(store, n=5)
        scanner = OpportunityScanner(repository, store, settings=type(
            "S", (), {"commission": 0.0005, "slippage": 0.0002,
                      "spread": 0.0005, "scanner_top_k": 5})())
        evidence = {"momentum": {"oos_expectancy": 0.9, "oos_trades": 8}}
        assert scanner.scan(["NSE_EQ|AAA"], [REGISTRY["momentum"]()],
                            evidence=evidence) == []

    def test_top_k_and_exposure_penalty(self, env):
        repository, store = env
        self._store_series(store, key="NSE_EQ|AAA")
        self._store_series(store, key="NSE_EQ|BBB")
        scanner = OpportunityScanner(repository, store, settings=type(
            "S", (), {"commission": 0.0005, "slippage": 0.0002,
                      "spread": 0.0005, "scanner_top_k": 1})())
        evidence = {"momentum": {"oos_expectancy": 0.9, "oos_trades": 8}}
        hot = scanner.scan(["NSE_EQ|AAA"], [REGISTRY["momentum"]()],
                           evidence=evidence, current_exposure=0.0)
        capped = scanner.scan(["NSE_EQ|AAA"], [REGISTRY["momentum"]()],
                              evidence=evidence, current_exposure=0.9)
        assert len(hot) <= 1 and hot
        assert capped[0]["score"] < hot[0]["score"], "exposure lowers quality"
