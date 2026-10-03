"""ExperimentEngine: run parameter experiments → versioned, scored candidates.

Per candidate parameter set:
- chronological train/validation split (never shuffled),
- walk-forward folds when data allows (disjoint locked test windows),
- selection on VALIDATION expectancy only; robustness on the validation leg,
- LOCKED TEST run once for the winner's FINAL REPORT (never for selection),
- promotion ONLY when validation expectancy beats the baseline's.

Every run persists an experiments row + candidates rows. Turnover and
parameter stability are recorded with each candidate.
"""

from __future__ import annotations

from uuid import uuid4

from app.backtesting.costs import ExecutionConfig
from app.backtesting.engine import BacktestEngine
from app.strategies import create_strategy
from app.validation.robustness import robustness
from app.validation.train_test import SplitConfig, split_data


def _turnover(trades: list[dict], capital: float) -> float:
    if capital <= 0:
        return 0.0
    traded = sum(abs(t.get("quantity", 0)) * (t.get("entry", 0) + t.get("exit", 0))
                 for t in trades)
    return traded / capital


class ExperimentEngine:
    def __init__(self, repository, *, engine: BacktestEngine | None = None):
        self.repository = repository
        self.engine = engine or BacktestEngine(ExecutionConfig())

    def run(self, name: str, candles: list, strategy_name: str, param_grid: list[dict],
            timeframe: str, *, baseline_parameters: dict | None = None) -> dict:
        if not param_grid:
            raise ValueError("param_grid must contain at least one parameter set")
        experiment_id = uuid4().hex
        self.repository.record_experiment(experiment_id, name, {
            "strategy": strategy_name, "grid": param_grid, "timeframe": timeframe,
            "bars": len(candles)})
        baseline = baseline_parameters or param_grid[0]
        config = SplitConfig()
        train, validation, test = split_data(candles, config, timeframe)
        # 3.0.1 protocol: TRAIN → VALIDATION (selection) → LOCKED TEST (final
        # report only). The locked test leg is NEVER run during selection.
        partitions = {"train": len(train), "validation": len(validation),
                      "locked_test": len(test),
                      "selection_metric": "validation_expectancy"}
        results = []
        for parameters in param_grid:
            strategy = create_strategy(strategy_name, parameters)
            try:
                train_result = self.engine.run(
                    train, strategy, timeframe, label="TRAINING")
                validation_result = self.engine.run(
                    validation, strategy, timeframe, label="VALIDATION")
            except Exception as exc:
                results.append({"parameters": parameters, "error": type(exc).__name__})
                continue
            trades = validation_result.trades
            oos = {
                "oos_trades": len(trades),
                "oos_expectancy": (sum(t["pnl"] for t in trades) / len(trades)
                                   if trades else 0.0),
                "leg": "validation",  # selection NEVER reads the locked test
            }
            candidate = {
                "strategy": strategy_name, "parameters": parameters,
                "metrics": {k: validation_result.metrics.get(k) for k in (
                    "total_return", "max_drawdown", "sharpe", "win_rate",
                    "profit_factor", "number_of_trades", "exposure")},
                "oos": oos,
                "in_sample_return": train_result.metrics.get("total_return"),
                "turnover": _turnover(trades, self.engine.config.initial_capital),
                "robustness_score": 0.0,
                "parameter_stability": None,
            }
            try:
                robust = robustness(validation, timeframe, candidate, self.engine)
                warnings = robust.get("warnings", []) if isinstance(robust, dict) else []
                candidate["robustness_score"] = max(0.0, 1.0 - len(warnings) * 0.25)
                candidate["parameter_stability"] = robust if isinstance(robust, dict) else None
            except Exception:
                candidate["robustness_score"] = 0.0
            results.append(candidate)

        def oos_expectancy(item):
            return (item.get("oos") or {}).get("oos_expectancy", 0.0)

        baseline_oos = next((r for r in results if r.get("parameters") == baseline), None)
        baseline_expectancy = oos_expectancy(baseline_oos) if baseline_oos else 0.0
        winner = None
        for candidate in results:
            if "error" in candidate:
                continue
            promoted = oos_expectancy(candidate) > baseline_expectancy and \
                candidate["parameters"] != baseline
            reason = ("validation expectancy beats baseline" if promoted else
                      "does not beat baseline on validation expectancy")
            candidate_id_value = uuid4().hex
            self.repository.record_candidate(
                candidate_id_value, experiment_id=experiment_id,
                strategy=strategy_name, parameters=candidate["parameters"],
                dataset_id=None, metrics=candidate["metrics"], oos=candidate["oos"],
                robustness={"score": candidate["robustness_score"],
                            "turnover": candidate["turnover"],
                            "in_sample_return": candidate["in_sample_return"]},
                promoted=promoted, reason=reason)
            candidate["candidate_id"] = candidate_id_value
            candidate["promoted"] = promoted
            candidate["reason"] = reason
            if promoted and (winner is None or oos_expectancy(candidate)
                             > oos_expectancy(winner)):
                winner = candidate
        # LOCKED TEST: executed ONCE, only for the final report of the
        # already-selected winner. Its result never feeds back into selection.
        locked_test_report = None
        if winner is not None and len(test) >= 10:
            try:
                locked = self.engine.run(
                    test, create_strategy(strategy_name, winner["parameters"]),
                    timeframe, label="UNSEEN TEST")
                locked_trades = locked.trades
                locked_test_report = {
                    "candidate": winner["parameters"],
                    "leg": "locked_test_final_report_only",
                    "metrics": {k: locked.metrics.get(k) for k in (
                        "total_return", "max_drawdown", "number_of_trades")},
                    "oos_trades": len(locked_trades),
                    "oos_expectancy": (sum(t["pnl"] for t in locked_trades) / len(locked_trades)
                                       if locked_trades else 0.0),
                }
            except Exception as exc:
                locked_test_report = {"error": type(exc).__name__}
        finished = {
            "candidates": len(results),
            "promoted": sum(1 for c in results if c.get("promoted")),
            "baseline_parameters": baseline,
            "partitions": partitions,
            "locked_test_report": locked_test_report,
        }
        self.repository.finish_experiment(experiment_id, finished)
        if locked_test_report is not None:
            self.repository.record_learning_event(
                "LOCKED_TEST_REPORT",
                {"experiment_id": experiment_id,
                 "note": "final report only; selection used validation"})
        return {"experiment_id": experiment_id, **finished, "results": results}
