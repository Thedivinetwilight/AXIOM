"""Sensitivity diagnostics. Do not tune parameters to these test results."""

from dataclasses import replace
from app.backtesting.engine import BacktestEngine
from app.strategies import create_strategy


def suspicious_warnings(
    metrics, training_metrics=None, *, minimum_trades=10, high_return=1.0, performance_gap=0.30
):
    warnings = []
    if metrics["total_return"] > high_return:
        warnings.append("Suspiciously high return: inspect data, fills and overfitting")
    if metrics["number_of_trades"] < minimum_trades:
        warnings.append("Very low trade count: risk statistics may be unreliable")
    if metrics["sharpe"] is None or metrics["profit_factor"] is None:
        warnings.append(
            "Undefined risk metrics (zero variance/no realized losses); not evidence of safety"
        )
    if (
        training_metrics
        and abs(training_metrics["total_return"] - metrics["total_return"]) > performance_gap
    ):
        warnings.append(
            "Large training/test return gap; sample lengths also affect this comparison"
        )
    return warnings


def robustness(
    candles,
    timeframe,
    candidate,
    engine,
    *,
    perturbation=0.1,
    cost_multiplier=2.0,
    sensitivity_threshold=0.15,
):
    if not 0 < perturbation < 1 or not 1 <= cost_multiplier <= 5:
        raise ValueError("Invalid robustness settings")
    name, params = candidate["strategy"], candidate["parameters"]
    baseline = engine.run(candles, create_strategy(name, params), timeframe, label="ROBUSTNESS")
    trials, warnings = [], suspicious_warnings(baseline.metrics)
    variations = []
    for key, value in params.items():
        for direction in (-1, 1):
            changed = dict(params)
            changed[key] = (
                max(1, int(round(value * (1 + direction * perturbation))))
                if isinstance(value, int)
                else value * (1 + direction * perturbation)
            )
            if changed[key] == value:
                changed[key] = value + direction if isinstance(value, int) else value
            variations.append((f"parameter:{key}:{direction:+d}", changed, engine.config, candles))
    variations.append(
        (
            "commission_stress",
            params,
            replace(engine.config, commission=engine.config.commission * cost_multiplier),
            candles,
        )
    )
    variations.append(
        (
            "slippage_spread_stress",
            params,
            replace(
                engine.config,
                slippage=engine.config.slippage * cost_multiplier,
                spread=engine.config.spread * cost_multiplier,
            ),
            candles,
        )
    )
    for i in range(3):
        segment = candles[i * len(candles) // 3 : (i + 1) * len(candles) // 3]
        if len(segment) >= 2:
            direction = "up" if segment[-1].close > segment[0].close else "down_or_flat"
            variations.append((f"period_{i + 1}:{direction}", params, engine.config, segment))
    for label, parameters, config, bars in variations:
        try:
            strategy = create_strategy(name, parameters)
        except ValueError:
            warnings.append(f"Skipped invalid parameter perturbation: {label}")
            continue
        result = BacktestEngine(
            config, annualization=engine.annualization, calendar=engine.calendar
        ).run(bars, strategy, timeframe, label="ROBUSTNESS")
        delta = result.metrics["total_return"] - baseline.metrics["total_return"]
        trials.append(
            {
                "scenario": label,
                "parameters": parameters,
                "metrics": result.metrics,
                "return_delta": delta,
            }
        )
        if label.startswith("parameter:") and abs(delta) > sensitivity_threshold:
            warnings.append(f"Extreme parameter sensitivity: {label}")
    return {
        "label": "ROBUSTNESS DIAGNOSTICS",
        "baseline": baseline.metrics,
        "trials": trials,
        "warnings": warnings
        + [
            "Chronological period checks proxy differing conditions; no regime detector is implemented."
        ],
    }
