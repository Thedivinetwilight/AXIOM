"""Configurable bounded composite score; never rank by return alone."""

from dataclasses import dataclass, field
from math import isfinite, tanh

DEFAULT_WEIGHTS = {
    "return": 1.0,
    "sharpe": 1.0,
    "sortino": 0.5,
    "profit_factor": 0.5,
    "drawdown": 2.0,
    "low_trades": 1.0,
    "instability": 1.0,
    "volatility": 0.5,
    "costs": 1.0,
}


@dataclass(frozen=True)
class ScoringConfig:
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    minimum_trades: int = 10

    def __post_init__(self):
        if set(self.weights) != set(DEFAULT_WEIGHTS):
            raise ValueError("Provide exactly the documented scoring weight keys")
        if (isinstance(self.minimum_trades, bool)
                or not isinstance(self.minimum_trades, int) or self.minimum_trades < 1):
            raise ValueError("minimum_trades must be a positive integer")
        if any(not isfinite(v) or v < 0 for v in self.weights.values()):
            raise ValueError("Scoring weights must be finite/nonnegative")
        if not any(self.weights[k] for k in self.weights if k != "return"):
            raise ValueError("Return-only scoring is prohibited")


def score(metrics: dict, config: ScoringConfig | None = None) -> dict:
    config = config or ScoringConfig()
    count = metrics["number_of_trades"]
    profit_factor = metrics["profit_factor"]
    # Zero is real evidence (no gross winning P&L), not the same as an
    # undefined value. Only None receives the documented neutral treatment.
    normalized_profit_factor = 1.0 if profit_factor is None else profit_factor
    components = {
        "return": tanh(metrics["total_return"]),
        "sharpe": tanh((metrics["sharpe"] or 0) / 3),
        "sortino": tanh((metrics["sortino"] or 0) / 3),
        # Undefined PF (no losses) is neutral, not infinity/a free high score.
        "profit_factor": tanh((normalized_profit_factor - 1) / 2),
        "drawdown": -metrics["max_drawdown"],
        "low_trades": -max(0, 1 - count / config.minimum_trades),
        "instability": -metrics["instability"],
        "volatility": -metrics["volatility"],
        "costs": -metrics["cost_ratio"],
    }
    weighted = {key: value * config.weights[key] for key, value in components.items()}
    return {"score": sum(weighted.values()), "components": weighted}
