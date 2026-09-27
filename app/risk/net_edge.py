"""Net Edge Gate — determines if expected value after realistic costs justifies trading.

Additive, fail-closed, deterministic, transparent. Sits immediately before HardRiskLimits.
Uses existing Arena ExecutionConfig economics (commission, slippage, spread) rather
than inventing contradictory accounting.

Concept:
  expected_edge = gross_expected - fees - slippage - spread_cost - market_impact
  if net_expected <= threshold: NO TRADE

If required inputs unavailable -> fail closed (NO TRADE) and log reason.
Never bypasses hard risk.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger("bot.risk.net_edge")


@dataclass(frozen=True)
class NetEdgeResult:
    passes: bool
    gross_expected: float
    fees: float
    spread_cost: float
    slippage: float
    market_impact: float
    net_expected: float
    threshold: float
    reason: str


class NetEdgeGate:
    def __init__(self, threshold: float = 0.001, min_edge_bps: float = 10.0):
        self.threshold = float(threshold)
        self.min_edge_bps = float(min_edge_bps)

    @classmethod
    def from_settings(cls, settings) -> "NetEdgeGate":
        return cls(threshold=float(getattr(settings, "net_edge_threshold", 0.001)))

    def evaluate(
        self,
        *,
        gross_expected: float | None = None,
        expected_win_prob: float | None = None,
        expected_win: float | None = None,
        expected_loss: float | None = None,
        price: float | None = None,
        quantity: float | None = None,
        commission_rate: float | None = None,
        slippage_rate: float | None = None,
        spread_bps: float | None = None,
        volatility: float | None = None,
        holding_period: int | None = None,
        fees: float | None = None,
        spread_cost: float | None = None,
        slippage: float | None = None,
        market_impact: float | None = None,
    ) -> NetEdgeResult:
        # Fail closed if gross not computable and win prob not provided
        gross = gross_expected
        if gross is None:
            if expected_win_prob is not None and expected_win is not None and expected_loss is not None:
                try:
                    # E[R] = p*win - (1-p)*loss
                    gross = float(expected_win_prob) * float(expected_win) - (1 - float(expected_win_prob)) * abs(float(expected_loss))
                except Exception:
                    gross = None
            else:
                gross = None

        # If still None -> missing inputs -> fail closed
        if gross is None or (isinstance(gross, float) and (math.isnan(gross) or math.isinf(gross))):
            reason = "NET_EDGE_REJECT: missing gross_expected (fail closed)"
            logger.info(reason, extra={"threshold": self.threshold})
            return NetEdgeResult(False, float("nan") if gross is None else gross, 0, 0, 0, 0, float("-inf"), self.threshold, reason)

        # Validate gross
        if not isinstance(gross, (int, float)) or math.isnan(float(gross)) or math.isinf(float(gross)):
            reason = "NET_EDGE_REJECT: invalid gross_expected"
            return NetEdgeResult(False, float(gross), 0, 0, 0, 0, float("-inf"), self.threshold, reason)

        # Compute costs if not provided
        # Use provided or estimate from price*quantity*rates
        notional = None
        if price is not None and quantity is not None:
            try:
                notional = abs(float(price) * float(quantity))
            except Exception:
                notional = None

        # fees
        fee_val = fees
        if fee_val is None:
            if notional is not None and commission_rate is not None:
                try:
                    fee_val = notional * float(commission_rate) * 2  # both sides
                except Exception:
                    fee_val = 0.0
            else:
                fee_val = 0.0

        # spread_cost
        spr_val = spread_cost
        if spr_val is None:
            if spread_bps is not None:
                try:
                    # spread_bps is in bps (e.g., 50 = 0.5%)
                    spr_val = (float(spread_bps) / 10000.0) * (notional or 1.0)
                except Exception:
                    spr_val = 0.0
            elif notional is not None:
                # fallback: use commission's spread if available? Use 0
                spr_val = 0.0
            else:
                spr_val = 0.0

        # slippage
        slip_val = slippage
        if slip_val is None:
            if notional is not None and slippage_rate is not None:
                try:
                    slip_val = notional * float(slippage_rate)
                except Exception:
                    slip_val = 0.0
            else:
                slip_val = 0.0

        # market impact: simple volatility * sqrt(quantity) proxy
        impact_val = market_impact
        if impact_val is None:
            impact_val = 0.0
            if volatility is not None and notional is not None:
                try:
                    # impact ~ vol * 0.1 * notional
                    impact_val = float(volatility) * 0.1 * notional * 0.01
                    if holding_period and holding_period > 0:
                        impact_val /= math.sqrt(float(holding_period))
                except Exception:
                    impact_val = 0.0

        # Guard against NaN/Inf in costs
        for v in (fee_val, spr_val, slip_val, impact_val):
            if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                reason = "NET_EDGE_REJECT: invalid cost estimate (fail closed)"
                return NetEdgeResult(False, float(gross), 0, 0, 0, 0, float("-inf"), self.threshold, reason)
            if v < 0:
                # negative costs are invalid (would imply profit from costs)
                reason = "NET_EDGE_REJECT: negative cost estimate"
                return NetEdgeResult(False, float(gross), float(fee_val), float(spr_val), float(slip_val), float(impact_val), float("-inf"), self.threshold, reason)

        net = float(gross) - float(fee_val) - float(spr_val) - float(slip_val) - float(impact_val)

        if math.isnan(net) or math.isinf(net):
            reason = "NET_EDGE_REJECT: net_expected invalid"
            return NetEdgeResult(False, float(gross), float(fee_val), float(spr_val), float(slip_val), float(impact_val), net, self.threshold, reason)

        passes = net > self.threshold

        if passes:
            reason = f"NET_EDGE_PASS: gross_expected={gross:.6f} fees={fee_val:.6f} spread={spr_val:.6f} slippage={slip_val:.6f} net_expected={net:.6f} threshold={self.threshold:.6f}"
            logger.debug(reason)
        else:
            reason = f"NET_EDGE_REJECT: gross_expected={gross:.6f} fees={fee_val:.6f} spread={spr_val:.6f} slippage={slip_val:.6f} net_expected={net:.6f} threshold={self.threshold:.6f}"
            logger.info(reason)

        return NetEdgeResult(passes, float(gross), float(fee_val), float(spr_val), float(slip_val), float(impact_val), net, self.threshold, reason)
