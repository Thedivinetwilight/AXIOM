"""Rolling discovery and selection, with disjoint test windows."""

from dataclasses import dataclass
from app.tournament.selection import discover_and_select, LockedTest
from app.validation.robustness import suspicious_warnings
from app.backtesting.metrics import calculate
from app.data.validator import validate


@dataclass(frozen=True)
class WalkForwardConfig:
    train_bars: int = 360
    validation_bars: int = 120
    test_bars: int = 120
    step_bars: int = 120

    def __post_init__(self):
        values = (self.train_bars, self.validation_bars, self.test_bars, self.step_bars)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 2 for v in values):
            raise ValueError("Window sizes must be integers >= 2")
        if self.step_bars < self.test_bars:
            raise ValueError("Overlapping test windows are prohibited")


def walk_forward(
    candles,
    timeframe,
    engine,
    *,
    grids=None,
    scoring=None,
    config: WalkForwardConfig | None = None,
    shortlist=5,
):
    config = config or WalkForwardConfig()
    validate(candles, timeframe).require_valid()
    width = config.train_bars + config.validation_bars + config.test_bars
    if len(candles) < width:
        raise ValueError("Not enough data for one walk-forward window")
    folds, aggregate_curve, aggregate_trades = [], [], []
    capital = engine.config.initial_capital
    carried = capital
    total_fees, total_impact = 0.0, 0.0
    for start in range(0, len(candles) - width + 1, config.step_bars):
        a, b = start + config.train_bars, start + config.train_bars + config.validation_bars
        train, validation, test = candles[start:a], candles[a:b], candles[b : start + width]
        candidate, _ = discover_and_select(
            train, validation, timeframe, engine, grids, scoring, shortlist
        )
        result = LockedTest(test).evaluate(candidate, engine, timeframe, train + validation)
        factor = carried / capital
        for snapshot in result.equity_curve:
            aggregate_curve.append(
                {
                    **snapshot,
                    "equity": snapshot["equity"] * factor,
                    "fees": total_fees + snapshot["fees"] * factor,
                    "spread_slippage_cost": total_impact
                    + snapshot["spread_slippage_cost"] * factor,
                }
            )
        aggregate_trades.extend({**trade, "pnl": trade["pnl"] * factor} for trade in result.trades)
        carried *= 1 + result.metrics["total_return"]
        total_fees += result.metrics["commission_costs"] * factor
        total_impact += result.metrics["spread_slippage_costs"] * factor
        folds.append(
            {
                "fold": len(folds) + 1,
                "train_start": train[0].timestamp.isoformat(),
                "test_start": test[0].timestamp.isoformat(),
                "test_end": test[-1].available_at(timeframe).isoformat(),
                "selected_candidate": candidate,
                "test_metrics": result.metrics,
                "warnings": suspicious_warnings(result.metrics, candidate["training_metrics"]),
            }
        )
    aggregate = calculate(
        aggregate_curve, aggregate_trades, capital, annualization=engine.annualization
    )
    return {
        "label": "WALK-FORWARD UNSEEN TEST WINDOWS",
        "folds": folds,
        "aggregate_metrics": aggregate,
        "linked_test_equity": aggregate_curve,
        "warnings": [
            "Independent flat-start folds; linked marked returns are research statistics, not an executable continuous portfolio.",
            "CAGR omitted for discontinuous evaluation windows. Earlier test data may enter later training after becoming historical.",
            "Parameters, window lengths and grids must be frozen before inspecting test outcomes.",
        ],
    }
