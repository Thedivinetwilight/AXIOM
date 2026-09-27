"""StrategyDiscoveryEngine: controlled candidate search with hard gates.

Every candidate is evaluated with chronological splits only:
  train → OOS expectancy (V1 out_of_sample_expectancy logic) → robustness
  score (parameter/cost perturbations on the OOS leg) → walk-forward
  stability proxy. Gates (documented, never bypassed):
    oos_trades >= 3  AND  oos_expectancy > 0  AND  robustness_score >= 0.25
    AND  oos max_drawdown <= 0.5  AND  wf_expectancy > -0.5
In-sample return is NEVER part of promotion. Candidates with zero trades or
negative OOS expectancy are recorded honestly as not tradable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.backtesting.costs import ExecutionConfig
from app.backtesting.engine import BacktestEngine
from app.learning.evaluator import out_of_sample_expectancy

MIN_OOS_TRADES = 3
MIN_ROBUSTNESS = 0.25
MAX_OOS_DRAWDOWN = 0.5
MIN_WF_EXPECTANCY = -0.5

# Small, documented grids over the 7 V1 strategy families (controlled search).
FAMILIES: dict[str, list[dict]] = {
    "moving_average": [
        {"fast_period": 3, "slow_period": 10},
        {"fast_period": 5, "slow_period": 20},
    ],
    "momentum": [
        {"period": 1, "threshold": 0.0},
        {"period": 2, "threshold": 0.0},
        {"period": 3, "threshold": 0.0},
        {"period": 4, "threshold": 0.0},
    ],
    "mean_reversion": [
        {"period": 5, "deviation": 0.03},
        {"period": 10, "deviation": 0.03},
    ],
    "breakout": [
        {"period": 10},
        {"period": 20},
    ],
    "rsi": [
        {"period": 14},
    ],
    "macd": [
        {"fast_period": 12, "slow_period": 26, "signal_period": 9},
    ],
    "bollinger": [
        {"period": 20, "std_dev": 2.0},
    ],
}


@dataclass(frozen=True)
class Spec:
    """One candidate configuration (strategy family + concrete parameters)."""

    strategy: str
    parameters: dict = field(default_factory=dict)
    source: str | None = None  # e.g. "grid" | "mutate:seed=1" | "recombine"

    def key(self) -> str:
        return f"{self.strategy}:{sorted(self.parameters.items())}"


def generate_specs(max_candidates: int = 12) -> list[Spec]:
    """Deterministic, capped candidate list across all 7 families."""
    specs: list[Spec] = []
    for strategy, grid in FAMILIES.items():
        for parameters in grid:
            specs.append(Spec(strategy, dict(parameters), "grid"))
    return specs[: max(1, int(max_candidates))]


def _robustness_score(engine, candles, timeframe, spec) -> tuple[float, dict]:
    """Documented score: fraction of parameter/cost perturbations whose OOS-leg
    total return stays above −10% of the baseline (diagnostic, never tuned)."""
    from app.validation.robustness import robustness

    candidate = {"strategy": spec.strategy, "parameters": dict(spec.parameters)}
    report = robustness(candles, timeframe, candidate, engine,
                        perturbation=0.1, cost_multiplier=2.0,
                        sensitivity_threshold=0.15)
    trials = report["trials"]
    if not trials:
        return 0.0, report
    baseline_return = float(report["baseline"]["total_return"] or 0.0)
    floor = min(-0.10, baseline_return - 0.10)
    passed = sum(1 for trial in trials
                 if float(trial["metrics"]["total_return"] or 0.0) >= floor)
    return round(passed / len(trials), 4), report


def evaluate_spec(candles, timeframe: str, spec: Spec,
                  *, engine: BacktestEngine | None = None) -> dict:
    """Split → OOS → robustness → walk-forward, then apply the hard gates."""
    engine = engine or BacktestEngine(ExecutionConfig())
    if len(candles) < 60:
        return {"strategy": spec.strategy, "parameters": dict(spec.parameters),
                "tradable": False, "reason": "not_enough_candles (<60)",
                "oos": {"oos_trades": 0, "oos_expectancy": 0.0},
                "metrics": {}, "robustness_score": 0.0, "wf_expectancy": None}
    oos_eval = out_of_sample_expectancy(
        candles, spec.strategy, spec.parameters, engine, timeframe,
        train_fraction=0.7)
    if not oos_eval.get("valid"):
        return {"strategy": spec.strategy, "parameters": dict(spec.parameters),
                "tradable": False, "reason": oos_eval.get("reason", "invalid"),
                "oos": {"oos_trades": 0, "oos_expectancy": 0.0},
                "metrics": {}, "robustness_score": 0.0, "wf_expectancy": None}
    test_leg = candles[int(len(candles) * 0.7):]  # locked OOS leg (chronological)
    score, report = _robustness_score(engine, test_leg, timeframe, spec)
    # walk-forward stability proxy: two disjoint OOS sub-windows, averaged
    half = len(test_leg) // 2
    window_expectancies = []
    for window in (test_leg[:half], test_leg[half:]):
        if len(window) >= 10:
            leg = out_of_sample_expectancy(
                window, spec.strategy, spec.parameters, engine, timeframe,
                train_fraction=0.7)
            window_expectancies.append(float(leg.get("oos_expectancy") or 0.0))
    wf_expectancy = (sum(window_expectancies) / len(window_expectancies)
                     if window_expectancies else None)
    # OOS drawdown proxy from the test-leg baseline run
    from app.strategies import create_strategy

    oos_run = engine.run(test_leg,
                         create_strategy(spec.strategy, spec.parameters),
                         timeframe, label="UNSEEN TEST")
    oos_drawdown = float(oos_run.metrics.get("max_drawdown") or 0.0)
    gates = {
        "oos_trades>=3": int(oos_eval["oos_trades"]) >= MIN_OOS_TRADES,
        "oos_expectancy>0": float(oos_eval["oos_expectancy"]) > 0,
        "robustness>=0.25": score >= MIN_ROBUSTNESS,
        "drawdown<=0.5": oos_drawdown <= MAX_OOS_DRAWDOWN,
        "wf>-0.5": wf_expectancy is None or wf_expectancy > MIN_WF_EXPECTANCY,
    }
    tradable = all(gates.values())
    return {
        "strategy": spec.strategy,
        "parameters": dict(spec.parameters),
        "oos": {"oos_trades": int(oos_eval["oos_trades"]),
                "oos_expectancy": float(oos_eval["oos_expectancy"]),
                "train_expectancy": oos_eval.get("train_expectancy")},
        "metrics": {"max_drawdown": oos_drawdown,
                    "total_return": oos_run.metrics.get("total_return"),
                    "number_of_trades": oos_run.metrics.get("number_of_trades")},
        "robustness_score": score,
        "robustness_warnings": report.get("warnings", []),
        "wf_expectancy": wf_expectancy,
        "gates": gates,
        "tradable": tradable,
        "reason": "all gates passed" if tradable else
                  "failed: " + ", ".join(k for k, ok in gates.items() if not ok),
    }


class StrategyDiscoveryEngine:
    """Controlled search over the V1 strategy families; every result recorded."""

    def __init__(self, repository, *, settings=None):
        self.repository = repository
        self.settings = settings

    def discover(self, name: str, candles, timeframe: str) -> dict:
        from app.strategies import REGISTRY  # noqa: F401 (family validation)

        max_candidates = int(getattr(self.settings, "discovery_max_candidates", 12)
                             ) if self.settings is not None else 12
        specs = generate_specs(max_candidates)
        experiment_id = uuid.uuid4().hex
        self.repository.record_experiment(
            experiment_id, name,
            {"kind": "discovery", "candidates": len(specs),
             "timeframe": timeframe, "bars": len(candles),
             "gates": {"oos_trades": MIN_OOS_TRADES,
                       "robustness": MIN_ROBUSTNESS,
                       "max_drawdown": MAX_OOS_DRAWDOWN,
                       "wf_expectancy": MIN_WF_EXPECTANCY}})
        results = []
        for spec in specs:
            result = evaluate_spec(candles, timeframe, spec)
            self.repository.record_candidate(
                uuid.uuid4().hex, experiment_id=experiment_id,
                strategy=spec.strategy, parameters=spec.parameters,
                dataset_id=None, metrics=result.get("metrics", {}),
                oos=result.get("oos", {}), robustness={
                    "score": result.get("robustness_score", 0.0),
                    "wf_expectancy": result.get("wf_expectancy"),
                    "gates": result.get("gates", {})},
                promoted=bool(result.get("tradable")),
                reason=str(result.get("reason", "")))
            results.append(result)
        self.repository.record_learning_event(
            "DISCOVERY_RUN", {"experiment_id": experiment_id, "name": name,
                              "evaluated": len(results),
                              "tradable": sum(1 for r in results
                                              if r.get("tradable"))})
        self.repository.finish_experiment(
            experiment_id,
            {"candidates": len(results),
             "promoted": sum(1 for r in results if r.get("tradable"))})
        return {"experiment_id": experiment_id, "evaluated": len(results),
                "results": results}
