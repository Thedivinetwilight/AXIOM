from itertools import product
from app.backtesting.engine import BacktestEngine
from app.strategies import create_strategy
from app.tournament.scoring import score

DEFAULT_GRIDS = {
    "moving_average": {"fast_period": [10, 20], "slow_period": [30, 50]},
    "momentum": {"period": [10, 20], "threshold": [0.01, 0.03]},
    "mean_reversion": {"period": [20], "deviation": [0.02, 0.04]},
    "breakout": {"period": [10, 20]},
    "rsi": {"period": [14], "lower": [20, 30, 35], "upper": [70, 80]},
    "macd": {"fast_period": [12], "slow_period": [26], "signal_period": [9]},
    "bollinger": {"period": [20, 50], "std_dev": [2, 2.5]},
}


def configurations(grids: dict, max_configurations=10_000) -> list[dict]:
    result = []
    for name, grid in grids.items():
        if not isinstance(grid, dict) or any(
            not isinstance(v, list) or not v for v in grid.values()
        ):
            raise ValueError("Grid values must be non-empty lists")
        keys = list(grid)
        for values in product(*(grid[key] for key in keys)):
            parameters = dict(zip(keys, values))
            create_strategy(name, parameters)  # Invalid parameters fail loudly.
            result.append({"strategy": name, "parameters": parameters})
            if len(result) > max_configurations:
                raise ValueError("Grid exceeds configuration limit")
    if not result:
        raise ValueError("Empty strategy grid")
    return result


class TournamentRunner:
    def __init__(self, engine: BacktestEngine | None = None, scoring=None):
        self.engine, self.scoring = engine or BacktestEngine(), scoring

    def run(
        self,
        datasets: dict[tuple[str, str], list],
        grids=None,
        *,
        label="BACKTEST",
        repository=None,
    ):
        """Bounded-memory sequential Cartesian runs; no automatic deployment."""
        candidates = configurations(grids if grids is not None else DEFAULT_GRIDS)
        if not datasets:
            raise ValueError("No datasets")
        rows = []
        for (symbol, timeframe), bars in datasets.items():
            if {b.symbol for b in bars} != {symbol}:
                raise ValueError("Dataset key/symbol mismatch")
            for candidate in candidates:
                strategy = create_strategy(candidate["strategy"], candidate["parameters"])
                result = self.engine.run(bars, strategy, timeframe, label=label)
                if repository:
                    repository.save_backtest(result)
                rows.append(
                    {
                        **candidate,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "backtest_id": result.id,
                        "metrics": result.metrics,
                        **score(result.metrics, self.scoring),
                        "label": label,
                    }
                )
        rows.sort(key=lambda row: row["score"], reverse=True)
        return {
            "label": "RESEARCH TOURNAMENT",
            "runs": len(rows),
            "ranking": rows,
            "selected_candidate": rows[0] if rows else None,
            "warnings": [
                "In-sample ranking only; selected candidate is NOT a validated profitability claim.",
                "Run holdout/walk-forward before considering paper deployment.",
            ],
        }
