"""StrategyRouter: pick among APPROVED candidates for the current regime.

Scoring (documented, deterministic):
  score = 0.5·norm(oos_expectancy) + 0.3·robustness + 0.2·confidence
- oos_expectancy: out-of-sample ONLY (in-sample return is never scored).
- robustness: from the robustness harness (parameter/cost stability), [0,1].
- confidence: min(1, oos_trades / min_trades).
Risk constraints are hard filters (max drawdown, min trades, min robustness).
If nothing qualifies, the router returns the fallback (no trading) rather than
the least-bad candidate.
"""

from __future__ import annotations

from math import isfinite


class StrategyRouter:
    def __init__(self, *, fallback=("momentum", {"period": 2, "threshold": 0.0}),
                 min_oos_trades: int = 3, max_oos_drawdown: float = 0.5,
                 min_robustness: float = 0.25):
        self.fallback = fallback
        self.min_oos_trades = int(min_oos_trades)
        self.max_oos_drawdown = float(max_oos_drawdown)
        self.min_robustness = float(min_robustness)

    def score(self, candidate: dict) -> float | None:
        oos = candidate.get("oos") or {}
        trades = int(oos.get("oos_trades") or 0)
        expectancy = float(oos.get("oos_expectancy") or 0.0)
        if trades < self.min_oos_trades:
            return None
        if not isfinite(expectancy) or expectancy <= 0:
            return None  # invalid/negative OOS expectancy is never tradable
        metrics = candidate.get("metrics") or {}
        drawdown = float(metrics.get("max_drawdown") or 0.0)
        if (not isfinite(drawdown) or drawdown < 0
                or drawdown > self.max_oos_drawdown):
            return None
        robustness = float(candidate.get("robustness_score", 0.0))
        if (not isfinite(robustness) or not 0.0 <= robustness <= 1.0
                or robustness < self.min_robustness):
            return None
        confidence = min(1.0, trades / max(self.min_oos_trades, 1))
        # Expectancy normalized by a modest per-trade anchor; sign dominates.
        norm_expectancy = max(-1.0, min(1.0, expectancy / 0.01))
        return 0.5 * norm_expectancy + 0.3 * robustness + 0.2 * confidence

    def select(self, regime: str | None, candidates: list[dict], *,
               degradation: dict[str, float] | None = None,
               calibration_discount: dict[str, float] | None = None) -> dict:
        """degradation: strategy → recent-minus-baseline expectancy (<=0 bad).
        calibration_discount: strategy → multiplier in [0.5, 1.0]. Both are
        documented penalties on the score; neither can bypass a hard filter."""
        degradation = degradation or {}
        calibration_discount = calibration_discount or {}
        scored = []
        for candidate in candidates:
            value = self.score(candidate)
            if value is None or value <= 0:
                continue
            penalty = 0.0
            recent_drop = degradation.get(candidate["strategy"])
            if recent_drop is not None and recent_drop < 0:
                if recent_drop <= -3.0:
                    continue  # SHADOW/RETIRED: hard exclusion, never tradable
                penalty += 0.5  # PROBATION halves the score
            discount = calibration_discount.get(candidate["strategy"], 1.0)
            value = value * discount - penalty
            if value > 0:
                scored.append((value, candidate))
        if not scored:
            return {"strategy": self.fallback[0], "parameters": dict(self.fallback[1]),
                    "confidence": 0.0, "reason": "no qualified candidate; fallback",
                    "regime": regime, "selected": False}
        scored.sort(key=lambda pair: pair[0], reverse=True)
        value, winner = scored[0]
        return {"strategy": winner["strategy"], "parameters": dict(winner["parameters"]),
                "confidence": round(value, 4), "reason": "highest documented score "
                "(OOS expectancy × calibration − degradation penalty; robustness "
                "gated; in-sample return never used)",
                "regime": regime, "selected": True}
