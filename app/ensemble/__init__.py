"""AXIOM Parallel Strategy Ensemble, Empirical Calibration & Portfolio Risk Allocator.

Hard safety & isolation invariants:
- Evaluates strictly on validated, timestamped, completed OHLCV bars.
- Never imports or invokes any live broker order client (Alpaca, Upstox GTT, MT5 order_send).
- Individual strategies emit advisory candidate signals only; orders can only be proposed
  by one per-market PortfolioRiskAllocator and (in LIVE-MARKET PAPER) simulated locally.
- Research, calibration, walk-forward weighting, benchmarks, and restarts never arm or
  activate any paper or live execution session.
"""

from app.ensemble.allocator import PortfolioAllocationConfig, PortfolioRiskAllocator
from app.ensemble.calibration import EmpiricalCalibrator, WalkForwardWeightLearner
from app.ensemble.registry import (
    STRATEGY_FAMILIES,
    BoundedParallelEvaluator,
    SharedIndicatorContext,
    StrategyRegistry,
    StrategyVariantSpec,
    validate_completed_bars,
)
from app.ensemble.service import StrategyEnsembleService

__all__ = [
    "STRATEGY_FAMILIES",
    "BoundedParallelEvaluator",
    "EmpiricalCalibrator",
    "PortfolioAllocationConfig",
    "PortfolioRiskAllocator",
    "SharedIndicatorContext",
    "StrategyEnsembleService",
    "StrategyRegistry",
    "StrategyVariantSpec",
    "WalkForwardWeightLearner",
    "validate_completed_bars",
]
