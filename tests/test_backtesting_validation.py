from dataclasses import dataclass, replace
import json
import pytest
from app.backtesting.engine import BacktestEngine
from app.strategies.base import Strategy, Signal
from app.strategies import create_strategy
from app.tournament.runner import TournamentRunner, configurations
from app.tournament.scoring import ScoringConfig, score
from app.tournament.selection import LockedTest, validate_candidate
from app.validation.train_test import SplitConfig, split_data
from app.validation.walk_forward import WalkForwardConfig, walk_forward
from app.validation.robustness import robustness, suspicious_warnings
from tests.conftest import candle


@dataclass
class AlwaysBuy(Strategy):
    name = "always_buy"

    def generate_signal(self, data):
        return Signal.BUY


@dataclass
class BuyThenExit(Strategy):
    name = "buy_then_exit"

    def generate_signal(self, data):
        return Signal.BUY if len(data) == 1 else Signal.EXIT


def test_next_open_not_signal_close(zero_cost):
    bars = [candle(0, 100), candle(1, 120), candle(2, 130)]
    result = BacktestEngine(zero_cost).run(bars, BuyThenExit())
    assert result.fills[0]["price"] == 120
    assert result.fills[0]["quantity"] == pytest.approx(1000 / 120)
    assert result.fills[1]["price"] == 130
    assert result.metrics["total_return"] == pytest.approx(130 / 120 - 1)
    assert result.metrics["number_of_trades"] == 1
    assert result.metrics["cagr"] is None
    json.dumps(result.to_dict(), allow_nan=False)


def test_last_signal_not_filled(zero_cost):
    result = BacktestEngine(zero_cost).run([candle()], AlwaysBuy())
    assert not result.fills
    assert result.orders[0]["status"] == "CANCELLED"


def test_no_future_effect_on_prefix(zero_cost):
    engine = BacktestEngine(zero_cost)
    prefix = [candle(i, 100 + i) for i in range(10)]
    short = engine.run(prefix, AlwaysBuy())
    long = engine.run(prefix + [candle(10, 99999)], AlwaysBuy())
    assert [s["equity"] for s in short.equity_curve] == [
        s["equity"] for s in long.equity_curve[:10]
    ]
    assert short.fills[0]["price"] == long.fills[0]["price"]


def test_drawdown_and_daily_aggregation(zero_cost):
    result = BacktestEngine(zero_cost).run(
        [candle(0, 100), candle(1, 100), candle(2, 80)], AlwaysBuy()
    )
    assert result.metrics["max_drawdown"] == pytest.approx(0.2)
    assert result.metrics["total_return"] == pytest.approx(-0.2)
    assert result.metrics["exposure"] == pytest.approx(2 / 3)
    assert result.daily_returns[-1]["return"] == pytest.approx(-0.2)
    assert any("marked" in warning for warning in result.warnings)


def test_invalid_warmup_and_multi_symbol(zero_cost):
    engine = BacktestEngine(zero_cost)
    with pytest.raises(ValueError):
        engine.run([candle()], AlwaysBuy(), warmup=[candle(2)])
    with pytest.raises(ValueError):
        engine.run([candle(), candle(1, symbol="ALT")], AlwaysBuy())


def test_splits(bars):
    train, validation, test = split_data(bars)
    assert (len(train), len(validation), len(test)) == (180, 60, 60)
    assert train[-1].timestamp < validation[0].timestamp < test[0].timestamp
    with pytest.raises(ValueError):
        SplitConfig(0.8, 0.2, 0.2)
    with pytest.raises(ValueError):
        split_data(bars[:5])
    with pytest.raises(ValueError):
        WalkForwardConfig(100, 20, 20, 10)


def test_selection_never_optimizes_test(bars, zero_cost):
    engine = BacktestEngine(zero_cost)
    grids = {"momentum": {"period": [5, 10], "threshold": [0.01, 0.03]}}
    selected = validate_candidate(bars, "1D", engine, grids=grids, shortlist=2)
    changed = bars[:240] + [
        replace(b, open=b.open * 10, high=b.high * 10, low=b.low * 10, close=b.close * 10)
        for b in bars[240:]
    ]
    other = validate_candidate(changed, "1D", engine, grids=grids, shortlist=2)
    assert selected["selected_candidate"] == other["selected_candidate"]
    assert selected["unseen_test"]["label"] == "UNSEEN TEST"
    locked = LockedTest(bars[240:])
    candidate = selected["selected_candidate"]
    locked.evaluate(candidate, engine, "1D", bars[:240])
    with pytest.raises(RuntimeError):
        locked.evaluate(candidate, engine, "1D", bars[:240])


def test_walk_forward(bars, zero_cost):
    result = walk_forward(
        bars,
        "1D",
        BacktestEngine(zero_cost),
        grids={"momentum": {"period": [5, 10]}},
        config=WalkForwardConfig(100, 40, 40, 40),
    )
    assert len(result["folds"]) == 4
    windows = result["folds"]
    for first, second in zip(windows, windows[1:]):
        assert first["test_end"] <= second["test_start"]
    assert result["aggregate_metrics"]["cagr"] is None


def test_cartesian_tournament(bars, zero_cost):
    datasets = {("DEMO", "1D"): bars, ("ALT", "1D"): [replace(b, symbol="ALT") for b in bars]}
    result = TournamentRunner(BacktestEngine(zero_cost)).run(
        datasets, {"momentum": {"period": [5, 10]}}
    )
    assert result["runs"] == 4
    assert result["selected_candidate"] == result["ranking"][0]
    assert "components" in result["selected_candidate"]
    with pytest.raises(ValueError):
        configurations({"momentum": {"period": []}})


def test_scoring_penalizes_risk_and_cost(bars, zero_cost):
    metrics = BacktestEngine(zero_cost).run(bars, create_strategy("momentum")).metrics
    risky = {
        **metrics,
        "max_drawdown": metrics["max_drawdown"] + 0.1,
        "cost_ratio": metrics["cost_ratio"] + 0.1,
    }
    assert score(metrics)["score"] > score(risky)["score"]
    weights = {k: 0 for k in ScoringConfig().weights}
    weights["return"] = 1
    with pytest.raises(ValueError):
        ScoringConfig(weights)


def test_robustness(bars, zero_cost):
    strategy = create_strategy("momentum")
    result = robustness(
        bars,
        "1D",
        {"strategy": strategy.name, "parameters": strategy.parameters},
        BacktestEngine(zero_cost),
    )
    scenarios = {r["scenario"] for r in result["trials"]}
    assert "commission_stress" in scenarios
    assert "slippage_spread_stress" in scenarios
    assert any(s.startswith("period_") for s in scenarios)
    warnings = suspicious_warnings(
        {"total_return": 2, "number_of_trades": 1, "sharpe": None, "profit_factor": None},
        {"total_return": 0},
    )
    assert len(warnings) == 4
