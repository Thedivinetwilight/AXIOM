"""V1: learning layer — regime labels, journal, validated promotion only."""

from datetime import datetime, timedelta, timezone

import pytest

from app.backtesting.costs import ExecutionConfig
from app.backtesting.engine import BacktestEngine
from app.config.settings import Settings
from app.data.models import Candle
from app.database.database import Database
from app.database.repository import Repository
from app.learning.evaluator import (
    BatchMetrics,
    evaluate_and_maybe_promote,
    evaluate_batch,
    out_of_sample_expectancy,
    propose_candidate,
)
from app.learning.journal import LearningJournal
from app.learning.regime import classify_regime


def candles(closes):
    base = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)
    return [
        Candle(base + timedelta(minutes=i),
               c * 0.999, c * 1.001, c * 0.998, c, 10000, "RELIANCE")
        for i, c in enumerate(closes)
    ]


@pytest.fixture()
def repo(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/learning.db")
    repository = Repository(database)
    yield repository
    database.close()


class TestRegime:
    def test_labels_are_deterministic_and_observable_only(self):
        rising = candles([100 + 0.5 * i for i in range(25)])
        assert classify_regime(rising) == classify_regime(list(reversed(rising))[::-1])
        assert classify_regime(rising).startswith("trend_up")
        flat = candles([100.0] * 25)
        assert classify_regime(flat).startswith("range")
        assert classify_regime(candles([100.0, 101.0])) == "insufficient_data"


class TestJournal:
    def test_records_signals_and_outcomes(self, repo):
        journal = LearningJournal(repo, params_version="pv1")
        regime = journal.record_signal(strategy="momentum", symbol="RELIANCE",
                                       timeframe="1m", signal="BUY", price=100.0,
                                       candles=candles([100.0] * 25))
        assert regime == classify_regime(candles([100.0] * 25))
        signals = repo.list_signals()
        assert signals[0]["strategy"] == "momentum"
        assert signals[0]["params_version"] == "pv1"
        oid = journal.record_outcome(strategy="momentum", symbol="RELIANCE",
                                     entry=100.0, exit=101.0, quantity=10, pnl=9.5,
                                     fees=0.5, slippage=0.0, duration_bars=5,
                                     outcome="win", regime=regime)
        assert oid
        events = [e for e in repo.list_system_events(limit=50)
                  if e["kind"] == "TRADE_OUTCOME"]
        assert events and events[0]["payload"]["pnl"] == 9.5


class TestEvaluator:
    def test_batch_metrics_and_min_samples(self):
        rows = [{"regime": "r", "pnl": p} for p in (10, -5, 7, 3, -2)]
        metrics = evaluate_batch(rows)
        assert metrics["r"].samples == 5
        assert metrics["r"].win_rate == 0.6
        assert metrics["r"].expectancy == 2.6
        sparse = evaluate_batch([{"regime": "r", "pnl": 1}])
        assert sparse["r"].samples == 1 and sparse["r"].expectancy == 0.0

    def test_oos_expectancy_uses_chronological_split(self, repo):
        series = candles([100 + (0.4 if i % 3 else -0.2) * (1 + i * 0.01)
                          for i in range(60)])
        result = out_of_sample_expectancy(series, "momentum", {"period": 2, "threshold": 0.0},
                                          BacktestEngine(ExecutionConfig()), "1m")
        assert result["valid"] and "oos_expectancy" in result
        assert result["oos_trades"] <= len(series) // 2

    def test_promotion_requires_strict_oos_improvement(self, repo):
        series = candles([100 + (0.4 if i % 3 else -0.2) * (1 + i * 0.01)
                          for i in range(60)])
        engine = BacktestEngine(ExecutionConfig())
        decision = evaluate_and_maybe_promote(
            repo, series, "momentum", {"period": 2, "threshold": 0.0}, engine, "1m",
            candidate_parameters={"period": 3, "threshold": 0.0})
        versions = repo.list_model_versions()
        assert any(v["version"].startswith("momentum") for v in versions)
        if decision["promoted"]:
            cand = decision["candidate"]
            cur = decision["current"]
            assert cand["oos_expectancy"] > cur["oos_expectancy"]
            assert cand["oos_trades"] >= 3
        # Baseline recording is comparison evidence only, never a promotion.
        decision2 = evaluate_and_maybe_promote(
            repo, series, "momentum", {"period": 2, "threshold": 0.0}, engine, "1m")
        assert decision2["promoted"] is False
        assert decision2["promoted_version"] is None
        assert decision2["baseline_reference_only"] is True
        baseline = next(v for v in repo.list_model_versions()
                        if v["version"] == "momentum-baseline")
        assert baseline["promoted"] is False

    def test_propose_candidate_is_deterministic(self):
        params = {"period": 2}
        assert propose_candidate(params) == {"period": 3}
        assert propose_candidate(params, delta=-1) == {"period": 1}
        assert params == {"period": 2}, "input must not be mutated"

    def test_learning_never_touches_safety_settings(self, repo):
        before = Settings()
        journal = LearningJournal(repo)
        journal.record_signal(strategy="momentum", symbol="RELIANCE", timeframe="1m",
                              signal="BUY", price=100.0, candles=candles([100.0] * 25))
        series = candles([100 + i for i in range(30)])
        evaluate_and_maybe_promote(repo, series, "momentum", {"period": 2},
                                   BacktestEngine(ExecutionConfig()), "1m")
        after = Settings()
        assert (after.broker, after.live_trading, after.mode) == \
               (before.broker, before.live_trading, before.mode)
        assert (after.max_daily_loss, after.max_trades_per_day) == \
               (before.max_daily_loss, before.max_trades_per_day)
