"""FINAL: market memory, calibration, portfolio intelligence, router.

Written against the ACTUAL V2.1/FINAL APIs (memory.record/similar dict,
ConfidenceCalibration.record/reliability/discount, CapitalAllocator.allocate,
StrategyRouter.score/select with degradation + calibration penalties).
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.database.database import Database
from app.database.repository import Repository
from app.data.models import Candle
from app.learning.calibration import ConfidenceCalibration
from app.learning.memory import DISCLAIMER, MarketMemory
from app.learning.portfolio import CapitalAllocator, PortfolioIntelligence
from app.learning.router import StrategyRouter

BASE = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)


def series(n=120, drift=0.15, seed=9):
    rng = random.Random(seed)
    price = 100.0
    out = []
    for i in range(n):
        price *= 1 + rng.uniform(-0.003, 0.003) + drift * 0.001
        out.append(Candle(BASE + timedelta(minutes=i), price * 0.999,
                          price * 1.001, price * 0.998, price, 8000, "X"))
    return out


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/intel.db")
    repository = Repository(database)
    yield repository
    database.close()


class FakeLedger:
    cash = 60000.0
    realized_pnl = -362.42

    def equity(self):
        return 100000.0

    def unrealized(self):
        return 1000.0


class TestMemory:
    def test_record_and_similar_with_disclaimer(self, env):
        memory = MarketMemory(env)
        memory.record(BASE.isoformat(), "trend_up_normal",
                      {"trend": 1.0, "volatility": 0.2, "momentum": 0.5,
                       "volume_trend": 1.0}, instrument="X")
        memory.record((BASE + timedelta(minutes=1)).isoformat(), "range_low_vol",
                      {"trend": 0.0, "volatility": 0.1, "momentum": 0.0,
                       "volume_trend": 1.0}, instrument="X")
        result = memory.similar({"trend": 1.0, "volatility": 0.2,
                                 "momentum": 0.5, "volume_trend": 1.0},
                                k=2, instrument="X")
        assert result["matches"]
        assert result["matches"][0]["regime"] == "trend_up_normal"
        assert result["matches"][0]["distance"] == 0.0
        assert result["disclaimer"] == DISCLAIMER
        assert "NOT a guarantee" in DISCLAIMER

    def test_similar_with_no_history_is_empty(self, env):
        assert MarketMemory(env).similar({"trend": 0.0},
                                         instrument="X")["matches"] == []


class TestCalibration:
    def test_reliability_buckets_and_brier(self, env):
        calibration = ConfidenceCalibration(env)
        calibration.record("momentum", 0.9, True)
        calibration.record("momentum", 0.9, False)  # overconfident
        calibration.record("momentum", 0.6, True)
        stats = calibration.reliability("momentum")
        assert stats["n"] == 3
        assert 0 <= stats["brier"] <= 1
        labels = {b["bucket"] for b in stats["buckets"]}
        assert ">0.75" in labels and "0.5-0.75" in labels
        assert calibration.reliability("never-seen")["n"] == 0

    def test_discount_bounded_and_never_inflates(self, env):
        calibration = ConfidenceCalibration(env)
        assert calibration.discount("uncalibrated") == 1.0
        for _ in range(4):
            calibration.record("momentum", 0.9, False)
            calibration.record("momentum", 0.9, True)
        discount = calibration.discount("momentum")
        assert 0.5 <= discount <= 1.0


class TestPortfolio:
    def test_snapshot_from_real_ledger_shape(self, env):
        intelligence = PortfolioIntelligence(env)
        snapshot = intelligence.snapshot(
            FakeLedger(),
            open_positions=[{"quantity": 100, "price": 200.0,
                             "avg_price": 190.0}])
        assert snapshot["equity"] == 100000.0
        assert snapshot["gross_exposure"] == pytest.approx(20000.0)
        assert snapshot["exposure_ratio"] == pytest.approx(0.2)
        assert snapshot["max_concentration"] == pytest.approx(0.2)
        assert snapshot["positions"] == 1
        assert snapshot["realized_pnl"] == pytest.approx(-362.42)
        assert snapshot["unrealized_pnl"] == pytest.approx(1000.0)

    def test_allocator_weights_and_hard_cap(self, env):
        class Settings:
            max_strategy_allocation = 0.35

        allocator = CapitalAllocator(env, Settings())
        assert allocator.hard_cap == 0.35
        plan = allocator.allocate(
            [{"strategy": "a", "oos": {"oos_expectancy": 0.02, "oos_trades": 10},
              "robustness_score": 0.8},
             {"strategy": "b", "oos": {"oos_expectancy": 0.01, "oos_trades": 5},
              "robustness_score": 0.4}],
            total_capital=100000)
        assert plan["allocations"]["a"] > plan["allocations"]["b"]
        assert plan["allocations"]["a"] <= 35000.0
        # cap is PER STRATEGY (gross exposure stays the risk layer's job)
        assert max(plan["allocations"].values()) <= 35000.0
        assert "learning cannot" in plan["note"]

    def test_allocator_ignores_negative_or_thin_evidence(self, env):
        allocator = CapitalAllocator(env, type("S", (), {"max_strategy_allocation": 0.35})())
        plan = allocator.allocate(
            [{"strategy": "bad", "oos": {"oos_expectancy": -0.01, "oos_trades": 10},
              "robustness_score": 0.9},
             {"strategy": "thin", "oos": {"oos_expectancy": 0.02, "oos_trades": 1},
              "robustness_score": 0.9}],
            total_capital=100000)
        assert plan["allocations"] == {}


class TestRouter:
    def _candidate(self, expectancy=0.02, trades=10, robustness=0.5, name="s"):
        return {"strategy": name, "parameters": {"period": 2},
                "oos": {"oos_expectancy": expectancy, "oos_trades": trades},
                "metrics": {"max_drawdown": 0.2},
                "robustness_score": robustness}

    def test_negative_oos_is_not_tradable(self):
        router = StrategyRouter()
        assert router.score(self._candidate(expectancy=-0.02)) is None
        result = router.select("trend_up_normal",
                               [self._candidate(expectancy=-0.02)])
        assert result["selected"] is False and result["confidence"] == 0.0

    def test_min_trades_and_drawdown_hard_filters(self):
        router = StrategyRouter()
        assert router.score(self._candidate(trades=2)) is None
        candidate = self._candidate()
        candidate["metrics"]["max_drawdown"] = 0.9
        assert router.score(candidate) is None

    def test_prefers_stronger_evidence(self):
        router = StrategyRouter()
        winner = router.select("range_low_vol",
                               [self._candidate(robustness=0.1, name="weak"),
                                self._candidate(robustness=0.9, name="strong")])
        assert winner["selected"] and winner["strategy"] == "strong"

    def test_degradation_penalty_and_calibration_discount(self):
        router = StrategyRouter()
        clean = router.select("range_low_vol", [self._candidate(name="s")])
        degraded = router.select("range_low_vol", [self._candidate(name="s")],
                                 degradation={"s": -0.4})
        discounted = router.select("range_low_vol", [self._candidate(name="s")],
                                   calibration_discount={"s": 0.5})
        assert degraded["confidence"] < clean["confidence"]
        assert discounted["confidence"] < clean["confidence"]
        # degradation + 0.5 calibration can push the score non-positive → fallback
        wiped = router.select("range_low_vol", [self._candidate(name="s")],
                              degradation={"s": -0.5},
                              calibration_discount={"s": 0.5})
        assert wiped["selected"] is False

    def test_in_sample_return_never_scored(self):
        router = StrategyRouter()
        flat = self._candidate()
        inflated = self._candidate()
        inflated["metrics"]["in_sample_return"] = 10_000.0  # ignored by design
        assert router.score(flat) == router.score(inflated)
