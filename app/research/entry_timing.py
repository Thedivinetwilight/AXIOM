"""Optimal Entry Timing — extends strategy evaluation to distinguish TRADE NOW / WAIT / NO_TRADE.

Records historical outcomes for immediate, next candle, confirmation, pullback, breakout.
Uses existing chronological/OOS research infrastructure, no lookahead.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class TimingStats:
    entry_type: str
    trades: int = 0
    wins: int = 0
    total_return: float = 0.0
    avg_return: float = 0.0
    win_rate: float = 0.0
    expectancy: float = 0.0


@dataclass
class TimingDecision:
    action: str  # TRADE_NOW / WAIT / NO_TRADE
    preferred_entry: str
    confidence: float
    reasons: List[str] = field(default_factory=list)
    stats: Dict[str, TimingStats] = field(default_factory=dict)


class EntryTimingEngine:
    # Entry types we track
    TYPES = ["immediate", "next_candle", "confirmation", "pullback", "breakout"]

    def __init__(self, lookback: int = 50, min_trades: int = 10):
        self.lookback = int(lookback)
        self.min_trades = int(min_trades)
        # history: list of dicts {entry_type, return, timestamp}
        self._history: List[dict] = []

    @classmethod
    def from_settings(cls, settings):
        return cls(lookback=int(getattr(settings, "entry_timing_lookback", 50)))

    def record_outcome(self, entry_type: str, ret: float, timestamp: str | None = None, strategy: str | None = None):
        if entry_type not in self.TYPES:
            return
        if ret is None or math.isnan(ret) or math.isinf(ret):
            return
        self._history.append({"entry_type": entry_type, "return": float(ret), "ts": timestamp, "strategy": strategy})
        # bounded
        if len(self._history) > 1000:
            self._history = self._history[-1000:]

    def _stats_for(self, entry_type: str, strategy: str | None = None) -> TimingStats:
        relevant = [h for h in self._history if h["entry_type"] == entry_type and (strategy is None or h.get("strategy") == strategy)]
        # use lookback
        relevant = relevant[-self.lookback:]
        if not relevant:
            return TimingStats(entry_type, 0, 0, 0, 0, 0, 0)
        trades = len(relevant)
        wins = sum(1 for h in relevant if h["return"] > 0)
        total = sum(h["return"] for h in relevant)
        avg = total / trades if trades else 0
        win_rate = wins / trades if trades else 0
        # expectancy = avg (already)
        # For this simple version, expectancy = avg
        # Could also compute p*win - (1-p)*loss
        return TimingStats(entry_type, trades, wins, total, avg, win_rate, avg)

    def evaluate(self, *, strategy: str | None = None, current_volatility: float | None = None, market_context: dict | None = None) -> TimingDecision:
        stats = {t: self._stats_for(t, strategy) for t in self.TYPES}
        # Need at least min_trades for any type to consider waiting
        # Determine best entry by expectancy
        best = None
        best_exp = float("-inf")
        for t, s in stats.items():
            if s.trades >= self.min_trades and s.expectancy > best_exp:
                best_exp = s.expectancy
                best = t

        reasons = []
        if best is None:
            # Not enough data -> immediate is default, but check if any has positive expectancy with fewer trades
            # Fallback to immediate
            reasons.append("insufficient_sample: default immediate")
            return TimingDecision("TRADE_NOW", "immediate", 0.5, reasons, stats)

        # If best is immediate and its expectancy > 0, trade now
        immediate = stats["immediate"]
        if best == "immediate" and immediate.expectancy > 0:
            reasons.append(f"immediate best expectancy {immediate.expectancy:.4f}")
            return TimingDecision("TRADE_NOW", "immediate", 0.7, reasons, stats)

        # If waiting improves edge significantly (>20% better) and preserves execution quality, prefer waiting
        if best != "immediate":
            # Check if waiting historically destroys edge? Compare
            if immediate.trades >= self.min_trades:
                if best_exp > immediate.expectancy * 1.2 and best_exp > 0:
                    reasons.append(f"waiting {best} improves expectancy {best_exp:.4f} vs immediate {immediate.expectancy:.4f}")
                    # Also check volatility: if high vol, waiting may be risky
                    if current_volatility and current_volatility > 0.03:
                        reasons.append("high_vol_defer_wait")
                        return TimingDecision("WAIT", best, 0.6, reasons, stats)
                    return TimingDecision("WAIT", best, 0.65, reasons, stats)
                elif immediate.expectancy > 0 and best_exp <= immediate.expectancy:
                    reasons.append(f"immediate preserves edge {immediate.expectancy:.4f} vs {best} {best_exp:.4f}")
                    return TimingDecision("TRADE_NOW", "immediate", 0.6, reasons, stats)

        # If no advantage, trade now
        if best_exp <= 0:
            reasons.append(f"no_positive_edge best {best} {best_exp:.4f}")
            return TimingDecision("NO_TRADE", best, 0.4, reasons, stats)

        reasons.append(f"best entry {best} expectancy {best_exp:.4f}")
        return TimingDecision("WAIT" if best != "immediate" else "TRADE_NOW", best, 0.6, reasons, stats)

    def no_lookahead_check(self, decision_ts: str, data_ts: str) -> bool:
        # Ensure decision uses only data up to decision_ts
        # Simple string comparison (ISO); real would parse
        return decision_ts >= data_ts
