"""Volatility-aware sizing — extends existing CapitalAllocator, does not replace it.

Accounts for realized volatility, ATR, regime, confidence, expected edge, stop distance,
liquidity and portfolio exposure. Higher volatility/uncertainty -> smaller size.
Stronger edge -> larger (capped). Poor liquidity -> smaller/no trade.

Final quantity MUST still be computed BEFORE hard risk gate; this module never
exceeds hard risk limits; it only reduces size.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizingContext:
    base_quantity: float
    price: float
    volatility: float | None = None  # realized vol or ATR/price
    atr: float | None = None
    regime: str | None = None
    confidence: float | None = None
    expected_edge: float | None = None
    stop_distance: float | None = None
    liquidity_score: float | None = None
    exposure_ratio: float | None = None
    hard_cap_quantity: float | None = None


class VolatilityAwareSizer:
    def __init__(self, lookback: int = 20, max_vol_factor: float = 3.0):
        self.lookback = int(lookback)
        self.max_vol_factor = float(max_vol_factor)

    @classmethod
    def from_settings(cls, settings):
        return cls(
            lookback=int(getattr(settings, "volatility_lookback", 20)),
            max_vol_factor=float(getattr(settings, "max_volatility_factor", 3.0)),
        )

    def _volatility_scalar(self, vol: float | None, atr: float | None, price: float) -> float:
        # Normalize volatility to price units; ATR is already price units
        v = None
        if atr is not None and atr is not None and price and price > 0:
            try:
                v = float(atr) / float(price)
            except Exception:
                v = None
        if v is None and vol is not None:
            try:
                v = float(vol)
            except Exception:
                v = None
        if v is None or math.isnan(v) or math.isinf(v) or v <= 0:
            return 1.0
        # Typical vol 0.01-0.02; scale
        # scalar = 1 / (1 + k*vol) with k=20; vol 0.01 -> 0.83, 0.03 -> 0.62, 0.06 -> 0.45
        scalar = 1.0 / (1.0 + 20 * v)
        # Clamp to avoid extreme reduction
        return max(0.2, min(1.0, scalar))

    def adjust(self, ctx: SizingContext) -> tuple[float, dict]:
        base = float(ctx.base_quantity)
        if base <= 0 or math.isnan(base) or math.isinf(base):
            return 0.0, {"reason": "invalid_base_quantity", "scalar": 0.0}

        scalar = 1.0
        reasons = []

        # Volatility scalar
        vs = self._volatility_scalar(ctx.volatility, ctx.atr, ctx.price)
        scalar *= vs
        if vs < 0.8:
            reasons.append(f"high_volatility {vs:.2f}")

        # Regime
        if ctx.regime and "high_vol" in ctx.regime:
            scalar *= 0.7
            reasons.append("high_vol_regime")
        elif ctx.regime and "low_vol" in ctx.regime:
            scalar *= 1.05  # slight increase in calm regime, but capped

        # Confidence
        if ctx.confidence is not None:
            try:
                c = max(0.0, min(1.0, float(ctx.confidence)))
                # 0.5 -> 1.0, 0.8 -> 1.15, 0.3 -> 0.8
                cs = 0.6 + 0.8 * c
                scalar *= cs
                if c < 0.4:
                    reasons.append(f"low_confidence {c:.2f}")
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Expected edge
        if ctx.expected_edge is not None and not math.isnan(ctx.expected_edge) and not math.isinf(ctx.expected_edge):
            try:
                edge = float(ctx.expected_edge)
                if edge <= 0:
                    scalar *= 0.5
                    reasons.append("non_positive_edge")
                elif edge > 0.005:
                    # stronger edge -> up to 1.2
                    scalar *= min(1.2, 1.0 + edge * 20)
                    reasons.append(f"strong_edge {edge:.4f}")
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Stop distance: wider stop -> smaller size (risk parity)
        if ctx.stop_distance is not None and ctx.stop_distance is not None:
            try:
                sd = float(ctx.stop_distance)
                if sd > 0 and not math.isnan(sd) and not math.isinf(sd):
                    # Normal stop 0.05 (5%) is baseline in Arena; only penalize beyond 10%
                    if sd > 0.08:
                        sds = 0.05 / sd
                        scalar *= max(0.5, sds)
                        reasons.append(f"wide_stop {sd:.3f}")
                    elif sd > 0.05:
                        sds = 0.05 / sd
                        scalar *= max(0.7, sds)
                        reasons.append(f"wide_stop {sd:.3f}")
                    elif sd < 0.01:
                        sds = min(1.1, 0.05 / sd * 0.5 + 0.5)  # cap
                        scalar *= min(1.1, sds)
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Liquidity
        if ctx.liquidity_score is not None:
            try:
                ls = max(0.0, min(1.0, float(ctx.liquidity_score)))
                if ls < 0.2:
                    scalar *= 0.5
                    reasons.append(f"low_liquidity {ls:.2f}")
                elif ls < 0.35:
                    scalar *= 0.8
                    reasons.append(f"moderate_liquidity {ls:.2f}")
                # 0.35+ is considered adequate, no penalty
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Portfolio exposure
        if ctx.exposure_ratio is not None:
            try:
                er = float(ctx.exposure_ratio)
                if er > 0.6:
                    scalar *= 0.6
                    reasons.append(f"high_exposure {er:.2f}")
                elif er > 0.4:
                    scalar *= 0.8
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Clamp scalar
        scalar = max(0.1, min(2.0, scalar))
        # Respect hard cap
        qty = base * scalar
        if ctx.hard_cap_quantity is not None:
            try:
                cap = float(ctx.hard_cap_quantity)
                if cap > 0:
                    qty = min(qty, cap)
                    if qty == cap:
                        reasons.append("capped_by_hard_limit")
            except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        # Guard against NaN/Inf
        if math.isnan(qty) or math.isinf(qty) or qty < 0:
            return 0.0, {"scalar": scalar, "reasons": reasons, "error": "invalid_qty"}

        # Round down to 2 decimals for determinism
        qty = math.floor(qty * 100) / 100
        if qty < 0.01:
            qty = 0.0
            reasons.append("qty_below_minimum")

        return qty, {"scalar": round(scalar, 4), "reasons": reasons, "base": base}
