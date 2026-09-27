"""PortfolioIntelligence + CapitalAllocator.

Exposure/concentration snapshot from the live ledger, pairwise correlation
and strategy overlap from recorded fills, and evidence-based allocation.
Allocation is bounded by the HARD per-strategy cap (a risk setting learning
can never change); strategies with negative or thin out-of-sample evidence
receive NOTHING.
"""

from __future__ import annotations

import json
from statistics import correlation as _corr, fmean


class PortfolioIntelligence:
    def __init__(self, repository, store=None):
        self.repository = repository
        self.store = store

    def snapshot(self, ledger, *, open_positions: list[dict] | None = None) -> dict:
        open_positions = open_positions or []
        equity = ledger.equity()
        gross_exposure = sum(abs(float(p.get("quantity", 0)) * float(p.get("price")
                             or p.get("avg_price") or 0)) for p in open_positions)
        weights = [abs(float(p.get("quantity", 0)) * float(p.get("price")
                   or p.get("avg_price") or 0)) / equity for p in open_positions
                   if equity > 0]
        return {
            "equity": equity,
            "cash": ledger.cash,
            "gross_exposure": gross_exposure,
            "exposure_ratio": sum(weights),
            "max_concentration": max(weights, default=0.0),
            "positions": len(open_positions),
            "realized_pnl": ledger.realized_pnl,
            "unrealized_pnl": ledger.unrealized(),
        }

    def correlation(self, key_a: str, key_b: str, unit: str = "days",
                    interval: int = 1, lookback: int = 60) -> float | None:
        """Close-to-close return correlation between two instruments from
        recorded candles. None when there is insufficient overlap."""
        if self.store is None:
            return None
        a = self.store.read_range(key_a, unit, interval)[-lookback:]
        b = self.store.read_range(key_b, unit, interval)[-lookback:]
        if len(a) < 3 or len(b) < 3:
            return None
        n = min(len(a), len(b))
        ra = [float(a[i]["close"]) / float(a[i - 1]["close"]) - 1
              for i in range(len(a) - n + 1, len(a))]
        rb = [float(b[i]["close"]) / float(b[i - 1]["close"]) - 1
              for i in range(len(b) - n + 1, len(b))]
        try:
            return round(_corr(ra, rb), 6)
        except Exception:
            return None

    def strategy_overlap(self, strategy_a: str, strategy_b: str) -> float | None:
        """Jaccard overlap of the two strategies' recorded fill days."""
        days: dict[str, set] = {}
        for name in (strategy_a, strategy_b):
            fills = [e for e in self.repository.list_system_events(limit=2000)
                     if e["kind"] == "TRADE_OUTCOME"
                     and (e["payload"].get("strategy")
                          if isinstance(e["payload"], dict) else None) == name]
            days[name] = {str(e["ts"])[:10] for e in fills}
        union = days[strategy_a] | days[strategy_b]
        if not union:
            return None
        return round(len(days[strategy_a] & days[strategy_b]) / len(union), 6)


class CapitalAllocator:
    def __init__(self, repository, settings, calibration=None):
        self.repository = repository
        self.settings = settings
        self.calibration = calibration

    @property
    def hard_cap(self) -> float:
        return float(getattr(self.settings, "max_strategy_allocation", 0.35))

    def allocate(self, candidates: list[dict], *, total_capital: float) -> dict:
        """Weights ∝ max(0, OOS expectancy) × robustness × calibration,
        capped at the hard cap. Negative-evidence strategies get NOTHING."""
        raw = {}
        for candidate in candidates:
            oos = candidate.get("oos") or {}
            expectancy = float(oos.get("oos_expectancy") or 0.0)
            if expectancy <= 0 or int(oos.get("oos_trades") or 0) < 3:
                continue  # negative/insufficient evidence → zero allocation
            robustness = float(candidate.get("robustness_score") or 0.0)
            discount = self.calibration.discount(candidate["strategy"]) \
                if self.calibration else 1.0
            raw[candidate["strategy"]] = expectancy * robustness * discount
        total = sum(raw.values())
        allocations = {}
        if total > 0:
            for strategy, weight in raw.items():
                share = weight / total
                allocations[strategy] = round(min(share, self.hard_cap)
                                              * total_capital, 2)
        return {"allocations": allocations,
                "hard_cap_per_strategy": self.hard_cap,
                "note": "weights bounded by the hard risk cap; learning cannot "
                        "raise it; unvalidated strategies receive nothing"}
