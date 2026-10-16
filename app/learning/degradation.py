"""Strategy Degradation / Retirement — improves lifecycle monitoring.

Monitors rolling OOS expectancy, drawdown, trade quality, regime fit, hit rate,
profit factor, slippage sensitivity, alpha decay. Transitions ACTIVE->PROBATION->SHADOW/REVALIDATION->RETIRED.

Never retire from single bad trade, never promote from single good trade.
FULL research gate remains mandatory.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Dict


@dataclass
class HealthMetrics:
    strategy: str
    expectancy: float | None = None
    drawdown: float | None = None
    hit_rate: float | None = None
    profit_factor: float | None = None
    trade_quality: float | None = None
    regime_fit: float | None = None
    slippage_sensitivity: float | None = None
    alpha_decay_factor: float | None = None
    trades: int = 0


@dataclass
class DegradationDecision:
    action: str  # HOLD / PROBATION / SHADOW / RETIRE / REVALIDATE
    reason: str
    metrics: HealthMetrics
    evidence_trades: int


class DegradationMonitor:
    def __init__(self, min_trades_for_decision: int = 20, probation_threshold: float = -0.001, retire_threshold: float = -0.005):
        self.min_trades = int(min_trades_for_decision)
        self.probation_threshold = float(probation_threshold)
        self.retire_threshold = float(retire_threshold)
        # history: strategy -> list of expectancy points
        self._history: Dict[str, List[dict]] = {}

    @classmethod
    def from_settings(cls, settings):
        return cls()

    def record(self, strategy: str, metrics: HealthMetrics):
        self._history.setdefault(strategy, []).append({"metrics": metrics, "ts": len(self._history[strategy])})
        if len(self._history[strategy]) > 200:
            self._history[strategy] = self._history[strategy][-200:]

    def evaluate(self, metrics: HealthMetrics) -> DegradationDecision:
        # Never decide on insufficient trades
        if metrics.trades < self.min_trades:
            return DegradationDecision("HOLD", f"insufficient_trades {metrics.trades} < {self.min_trades}", metrics, metrics.trades)

        # Single bad trade does NOT retire: require sustained negative expectancy
        # Check expectancy
        exp = metrics.expectancy if metrics.expectancy is not None else 0
        if math.isnan(exp) or math.isinf(exp):
            exp = 0

        # Drawdown check
        dd = metrics.drawdown if metrics.drawdown is not None else 0
        if dd is None or math.isnan(dd) or math.isinf(dd):
            dd = 0

        # Hit rate
        hr = metrics.hit_rate if metrics.hit_rate is not None else 0.5

        # Profit factor
        pf = metrics.profit_factor if metrics.profit_factor is not None else 1.0

        # Trade quality
        tq = metrics.trade_quality if metrics.trade_quality is not None else 0.5

        # Regime fit
        rf = metrics.regime_fit if metrics.regime_fit is not None else 0.5

        # Slippage sensitivity: if high, penalize
        ss = metrics.slippage_sensitivity if metrics.slippage_sensitivity is not None else 0

        # Alpha decay
        ad = metrics.alpha_decay_factor if metrics.alpha_decay_factor is not None else 1.0

        # Decision funnel
        # RETIRE only if multiple metrics are bad AND expectancy strongly negative AND sufficient trades (>30)
        if metrics.trades >= 30 and exp < self.retire_threshold and pf < 0.8 and hr < 0.4 and tq < 0.3:
            return DegradationDecision("RETIRE", f"sustained negative expectancy {exp:.4f} pf {pf:.2f} hr {hr:.2f} tq {tq:.2f}", metrics, metrics.trades)

        # SHADOW: expectancy negative but not strongly, or drawdown large
        if exp < self.probation_threshold or dd > 0.15 or tq < 0.3:
            # If already degraded and still poor, go to SHADOW
            # For now, if expectancy < -0.001 and trades >= min, SHADOW
            if exp < self.probation_threshold and metrics.trades >= self.min_trades:
                return DegradationDecision("SHADOW", f"negative expectancy {exp:.4f} dd {dd:.2f}", metrics, metrics.trades)
            return DegradationDecision("PROBATION", f"degrading expectancy {exp:.4f} dd {dd:.2f} tq {tq:.2f}", metrics, metrics.trades)

        # Check additional degradation signals
        if rf < 0.3:
            return DegradationDecision("PROBATION", f"poor regime fit {rf:.2f}", metrics, metrics.trades)
        if ad < 0.6:
            return DegradationDecision("PROBATION", f"alpha decay {ad:.2f}", metrics, metrics.trades)
        if ss > 0.5:
            return DegradationDecision("PROBATION", f"high slippage sensitivity {ss:.2f}", metrics, metrics.trades)

        # Healthy
        return DegradationDecision("HOLD", f"healthy expectancy {exp:.4f} pf {pf:.2f}", metrics, metrics.trades)

    def should_revalidate(self, strategy: str) -> tuple[bool, str]:
        hist = self._history.get(strategy, [])
        if len(hist) < 10:
            return False, "insufficient_history"
        # If last 5 evaluations were SHADOW/RETIRE, suggest revalidation
        recent = hist[-5:]
        shadow_count = sum(1 for h in recent if h["metrics"].expectancy is not None and h["metrics"].expectancy < 0)
        if shadow_count >= 4:
            return True, f"{shadow_count}/5 recent negative expectancy → revalidation"
        return False, "no_revalidation_needed"

    def history(self, strategy: str) -> List[dict]:
        return self._history.get(strategy, [])
