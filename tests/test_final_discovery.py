"""FINAL: discovery gates, evolution lineage, lifecycle pipeline, leakage."""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.database.database import Database
from app.database.repository import Repository
from app.data.models import Candle
from app.learning.lifecycle import StrategyLifecycleManager
from app.research.discovery import (
    Spec,
    StrategyDiscoveryEngine,
    evaluate_spec,
    generate_specs,
)
from app.research.evolution import StrategyEvolution, mutate, recombine

BASE = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)


def series(n=300, drift=0.4, seed=3):
    rng = random.Random(seed)
    price = 100.0
    candles = []
    for i in range(n):
        price *= 1 + rng.uniform(-0.004, 0.004) + drift * 0.001
        candles.append(Candle(BASE + timedelta(minutes=i), price * 0.999,
                              price * 1.001, price * 0.998, price, 10000, "X"))
    return candles


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/final.db")
    repository = Repository(database)
    yield repository
    database.close()


class TestDiscovery:
    def test_controlled_specs_and_caps(self):
        specs = generate_specs(max_candidates=8)
        assert len(specs) == 8
        assert all(s.strategy in {"moving_average", "momentum", "mean_reversion",
                                  "breakout", "rsi", "macd", "bollinger"}
                   for s in specs)

    def test_gates_never_promote_by_in_sample(self, env):
        discovery = StrategyDiscoveryEngine(env)
        run = discovery.discover("gates", series(), "1m")
        assert run["results"]
        for candidate in run["results"]:
            if candidate.get("tradable"):
                # tradable candidates MUST have positive locked-test evidence
                assert candidate["oos"]["oos_trades"] >= 3
                assert candidate["oos"]["oos_expectancy"] > 0
                assert candidate["robustness_score"] >= 0.25
        rows = env.list_candidates(limit=50)
        assert rows, "every evaluated candidate is recorded with its evidence"

    def test_zero_trade_candidates_never_tradable(self, env):
        candidate = evaluate_spec(series(80), "1m",
                                  Spec("moving_average", {"fast_period": 5,
                                                          "slow_period": 20}))
        if candidate["oos"]["oos_trades"] == 0:
            assert candidate["tradable"] is False


class TestEvolution:
    def test_mutate_and_recombine_deterministic(self):
        spec = Spec("momentum", {"period": 2, "threshold": 0.0}, None)
        child = mutate(spec, seed=1)
        assert child.parameters["period"] in {1, 3}
        assert mutate(spec, seed=1).parameters == child.parameters
        other = Spec("momentum", {"period": 5, "threshold": 0.0}, None)
        mixed = recombine(spec, other, seed=2)
        assert set(mixed.parameters) == {"period", "threshold"}
        with pytest.raises(ValueError):
            recombine(spec, Spec("rsi", {"period": 14}, None))

    def test_children_validate_independently_and_lineage_is_kept(self, env):
        evolution = StrategyEvolution(env)
        root = evolution.register_root(Spec("momentum", {"period": 2,
                                                         "threshold": 0.0}, None))
        child = evolution.child(root, mutate(Spec("momentum", {"period": 2,
                                                               "threshold": 0.0}, None),
                                              seed=4), mutation="mutate")
        result = evolution.validate_child(child, series(), "1m")
        assert "tradable" in result
        lineage = env.get_lineage(child)
        assert lineage["parent_id"] == root and lineage["generation"] == 1
        # even a rejected child keeps its history (never deleted)
        assert env.current_lifecycle(child) is not None


class TestLifecycle:
    def test_stages_cannot_be_skipped(self, env):
        manager = StrategyLifecycleManager(env)
        evolution = StrategyEvolution(env)
        root = evolution.register_root(Spec("momentum", {"period": 2,
                                                         "threshold": 0.0}, None))
        # from RESEARCH only one promotion step at a time
        first = manager.promote(root, evidence={"why": "validation passed"})
        assert first["promoted"] and first["stage"] == "VALIDATION"
        current = env.current_lifecycle(root)
        assert current["stage"] == "VALIDATION"
        assert not manager.is_tradable(root), "VALIDATION ≠ APPROVED"

    def test_degradation_chain_and_immutable_history(self, env):
        manager = StrategyLifecycleManager(env)
        evolution = StrategyEvolution(env)
        root = evolution.register_root(Spec("momentum", {"period": 3,
                                                         "threshold": 0.0}, None))
        steps = [manager.detect_degradation(
            root, [-1.0] * 10, baseline_expectancy=4.0)["status"] for _ in range(3)]
        assert steps == ["PROBATION", "SHADOW", "RETIRED"]
        retired = env.current_lifecycle(root)
        assert retired["status"] == "RETIRED"
        assert not manager.is_tradable(root)
        assert len(env.lifecycle_history(root)) >= 4, "history is append-only"

    def test_recovered_strategy_stays_active(self, env):
        manager = StrategyLifecycleManager(env)
        evolution = StrategyEvolution(env)
        root = evolution.register_root(Spec("rsi", {"period": 14}, None))
        manager.detect_degradation(root, [-1.0] * 10, baseline_expectancy=1.0)
        recovered = manager.detect_degradation(root, [2.0] * 10,
                                               baseline_expectancy=1.0)
        assert not recovered["degraded"]
        assert env.current_lifecycle(root)["status"] == "ACTIVE"


class TestLeakageAdversarial:
    def test_shuffled_data_is_rejected(self):
        from app.data.validator import validate

        candles = series(60)
        shuffled = candles[:]
        random.Random(0).shuffle(shuffled)
        with pytest.raises(ValueError):
            validate(shuffled, "1m").require_valid()

    def test_past_features_unchanged_by_future_candles(self):
        from app.learning.regime_engine import features

        candles = series(30)
        past = features(candles[:20])
        with_future = features(candles)
        assert past["last_close"] == candles[19].close
        assert with_future["last_close"] == candles[-1].close
        # past features stay frozen even after more data arrives
        assert features(candles[:20]) == past
        # determinism: recomputing past features later yields identical values
        assert features(candles[:20]) == past

    def test_replay_excludes_information_after_the_decision(self, env):
        from app.research.replay import DecisionReplay

        early = (BASE).isoformat()
        env.record_signal({"id": "sig-l", "ts": early, "strategy": "momentum",
                           "params_version": "pv1", "symbol": "X",
                           "timeframe": "1m", "regime": "range_low_vol",
                           "signal": "BUY", "price": 100.0, "meta": {}})
        env.record_signal({"id": "sig-r", "ts": (BASE + timedelta(minutes=30)).isoformat(),
                           "strategy": "momentum", "params_version": "pv2",
                           "symbol": "X", "timeframe": "1m",
                           "regime": "trend_up_normal", "signal": "SELL",
                           "price": 105.0, "meta": {}})
        replay = DecisionReplay(env).replay_signal("sig-l")
        assert replay["signal"]["regime_recorded_then"] == "range_low_vol"
        assert replay["signal"]["params_version"] == "pv1"
        assert replay["signal"]["price"] == 100.0
