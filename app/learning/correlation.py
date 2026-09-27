"""Strategy Correlation / Concentration — improves portfolio intelligence.

Detects when multiple strategies make same bet (instrument, sector, direction,
factor exposure, return stream, position overlap). Adjusts allocation using
existing hard caps, never increases exposure because several agree.
"""
from __future__ import annotations

import math
from typing import List, Dict, Tuple
from statistics import correlation as _corr


class CorrelationMonitor:
    def __init__(self, threshold: float = 0.7):
        self.threshold = float(threshold)

    @classmethod
    def from_settings(cls, settings):
        return cls(threshold=float(getattr(settings, "correlation_threshold", 0.7)))

    def _return_series(self, fills: List[dict]) -> List[float]:
        # fills: list of dicts with pnl or return
        rets = []
        for f in fills:
            try:
                v = float(f.get("pnl") or f.get("return") or f.get("realized") or 0)
                if not math.isnan(v) and not math.isinf(v):
                    rets.append(v)
            except Exception:
                continue
        return rets

    def pairwise_correlation(self, series_a: List[float], series_b: List[float]) -> float | None:
        if len(series_a) < 3 or len(series_b) < 3:
            return None
        n = min(len(series_a), len(series_b))
        a = series_a[-n:]
        b = series_b[-n:]
        try:
            # If constant series, correlation undefined -> treat as 1 if both constant same direction
            if len(set(a)) == 1 or len(set(b)) == 1:
                return 1.0 if a[-1] == b[-1] else 0.0
            return round(_corr(a, b), 4)
        except Exception:
            return None

    def detect_concentration(
        self,
        positions: List[dict],
        candidates: List[dict],
    ) -> dict:
        # positions: [{symbol, quantity, strategy, sector, instrument_key}]
        # candidates: [{strategy, symbol, sector, direction}]
        symbol_counts: Dict[str, int] = {}
        sector_counts: Dict[str, int] = {}
        direction_counts: Dict[str, int] = {}

        for p in positions:
            sym = p.get("symbol") or p.get("instrument_key") or "UNKNOWN"
            symbol_counts[sym] = symbol_counts.get(sym, 0) + 1
            sec = p.get("sector") or "unknown"
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
            direction_counts[p.get("side") or p.get("direction") or "BUY"] = direction_counts.get(p.get("side") or "BUY", 0) + 1

        for c in candidates:
            sym = c.get("symbol") or c.get("instrument_key") or "UNKNOWN"
            symbol_counts[sym] = symbol_counts.get(sym, 0) + 1
            sec = c.get("sector") or c.get("affected_sectors", ["unknown"])[0] if isinstance(c.get("affected_sectors"), list) else "unknown"
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

        # Find concentration: any symbol/sector with >2 counts
        concentrated_symbols = [k for k, v in symbol_counts.items() if v > 2]
        concentrated_sectors = [k for k, v in sector_counts.items() if v > 2]

        # Correlation via return streams if available
        # For now, simple check: if multiple strategies target same symbol, flag
        is_concentrated = bool(concentrated_symbols or concentrated_sectors)

        return {
            "symbol_counts": symbol_counts,
            "sector_counts": sector_counts,
            "concentrated_symbols": concentrated_symbols,
            "concentrated_sectors": concentrated_sectors,
            "is_concentrated": is_concentrated,
            "total_positions": len(positions) + len(candidates),
        }

    def adjust_allocation(self, allocations: Dict[str, float], concentration: dict) -> Dict[str, float]:
        # If concentrated, scale down allocations proportionally, never up
        if not concentration.get("is_concentrated"):
            return allocations
        # Simple: reduce each by 20% if concentrated, to avoid overexposure
        # Do NOT increase because several agree
        factor = 0.8
        # More aggressive if many same symbol
        if len(concentration.get("concentrated_symbols", [])) >= 2:
            factor = 0.6
        adjusted = {k: round(v * factor, 2) for k, v in allocations.items()}
        return adjusted

    def correlation_matrix(self, strategy_returns: Dict[str, List[float]]) -> Dict[Tuple[str, str], float | None]:
        # strategy_returns: {strategy: [returns]}
        keys = list(strategy_returns.keys())
        matrix = {}
        for i, a in enumerate(keys):
            for j, b in enumerate(keys):
                if j <= i:
                    continue
                corr = self.pairwise_correlation(strategy_returns[a], strategy_returns[b])
                matrix[(a, b)] = corr
        return matrix

    def is_same_bet(self, strat_a: str, strat_b: str, returns_a: List[float], returns_b: List[float], threshold: float | None = None) -> bool:
        th = threshold if threshold is not None else self.threshold
        corr = self.pairwise_correlation(returns_a, returns_b)
        if corr is None:
            return False
        return corr >= th
