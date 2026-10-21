"""Net Edge Gate — determines whether a proposal clears realistic costs.

All P&L and cost arguments are monetary amounts in the same quote currency.
``threshold`` is a return fraction (for example 0.001 = 10 bps), so it is
converted to a monetary hurdle using the proposed position's notional before
comparison. If the proposal, expected P&L, cost inputs, or notional required
for that conversion is unavailable or invalid, the gate fails closed.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger("bot.risk.net_edge")


def _finite_number(value) -> float | None:
    """Return a finite real value, excluding bools and coercive strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class NetEdgeResult:
    passes: bool
    gross_expected: float | None
    fees: float
    spread_cost: float
    slippage: float
    market_impact: float
    net_expected: float | None
    threshold: float  # configured fractional hurdle (kept for compatibility)
    reason: str
    threshold_amount: float | None = None  # same currency as net_expected


class NetEdgeGate:
    def __init__(self, threshold: float = 0.001, min_edge_bps: float = 10.0):
        threshold_value = _finite_number(threshold)
        min_edge_value = _finite_number(min_edge_bps)
        if threshold_value is None or threshold_value < 0:
            raise ValueError("threshold must be a finite non-negative fraction")
        if min_edge_value is None or min_edge_value < 0:
            raise ValueError("min_edge_bps must be finite and non-negative")
        self.threshold = threshold_value
        # Kept as a legacy constructor parameter. The configured fractional
        # threshold is the single decision input; Settings documents its unit.
        self.min_edge_bps = min_edge_value

    @classmethod
    def from_settings(cls, settings) -> "NetEdgeGate":
        return cls(threshold=getattr(settings, "net_edge_threshold", 0.001))

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
        def reject(message: str, *, gross=None, fee=0.0, spread=0.0,
                   slip=0.0, impact=0.0, net=None, threshold_amount=None):
            reason = f"NET_EDGE_REJECT: {message}"
            logger.info(reason, extra={"threshold": self.threshold})
            return NetEdgeResult(
                passes=False,
                gross_expected=gross,
                fees=fee,
                spread_cost=spread,
                slippage=slip,
                market_impact=impact,
                net_expected=net,
                threshold=self.threshold,
                reason=reason,
                threshold_amount=threshold_amount,
            )

        # A gross estimate is monetary P&L for this proposal. Win/loss inputs
        # are accepted only when they form a finite probability-weighted amount.
        gross = _finite_number(gross_expected)
        if gross_expected is None:
            probability = _finite_number(expected_win_prob)
            win = _finite_number(expected_win)
            loss = _finite_number(expected_loss)
            if probability is None and expected_win_prob is None \
                    and expected_win is None and expected_loss is None:
                return reject("missing gross_expected (fail closed)")
            if (probability is None or win is None or loss is None
                    or not 0.0 <= probability <= 1.0 or win < 0.0):
                return reject("invalid expected-value inputs (fail closed)")
            gross = probability * win - (1.0 - probability) * abs(loss)
            if not math.isfinite(gross):
                return reject("invalid gross_expected (fail closed)")
        elif gross is None:
            return reject("invalid gross_expected (fail closed)")

        # Validate and calculate proposal notional. A positive fractional
        # threshold cannot be interpreted without this basis.
        notional = None
        if price is not None or quantity is not None:
            price_value = _finite_number(price)
            quantity_value = _finite_number(quantity)
            if (price_value is None or quantity_value is None
                    or price_value <= 0.0 or quantity_value <= 0.0):
                return reject("invalid price/quantity notional", gross=gross)
            notional = price_value * quantity_value
            if not math.isfinite(notional) or notional <= 0.0:
                return reject("invalid price/quantity notional", gross=gross)
        if self.threshold > 0.0 and notional is None:
            return reject(
                "missing notional for fractional threshold (fail closed)",
                gross=gross,
            )
        threshold_amount = self.threshold * notional if notional is not None else 0.0
        if not math.isfinite(threshold_amount):
            return reject("invalid threshold amount", gross=gross)

        def explicit_cost(value, name):
            parsed = _finite_number(value)
            if parsed is None or parsed < 0.0:
                return None, f"invalid {name} cost estimate (fail closed)"
            return parsed, None

        # Fees: explicit monetary estimate takes precedence; otherwise estimate
        # round-trip commission from notional and the per-side rate.
        if fees is not None:
            fee_value, error = explicit_cost(fees, "fee")
            if error:
                return reject(error, gross=gross, threshold_amount=threshold_amount)
        elif commission_rate is None:
            fee_value = 0.0
        else:
            rate = _finite_number(commission_rate)
            if rate is None or rate < 0.0:
                return reject("invalid commission rate (fail closed)", gross=gross,
                              threshold_amount=threshold_amount)
            if notional is None and rate != 0.0:
                return reject("missing notional for commission estimate",
                              gross=gross, threshold_amount=threshold_amount)
            fee_value = (notional or 0.0) * rate * 2.0
            if not math.isfinite(fee_value):
                return reject("invalid fee estimate (fail closed)", gross=gross,
                              threshold_amount=threshold_amount)

        # Spread: spread_bps is a full quoted spread, converted to currency.
        if spread_cost is not None:
            spread_value, error = explicit_cost(spread_cost, "spread")
            if error:
                return reject(error, gross=gross, fee=fee_value,
                              threshold_amount=threshold_amount)
        elif spread_bps is None:
            spread_value = 0.0
        else:
            bps = _finite_number(spread_bps)
            if bps is None or bps < 0.0:
                return reject("invalid spread estimate (fail closed)", gross=gross,
                              fee=fee_value, threshold_amount=threshold_amount)
            if notional is None and bps != 0.0:
                return reject("missing notional for spread estimate", gross=gross,
                              fee=fee_value, threshold_amount=threshold_amount)
            spread_value = (notional or 0.0) * bps / 10000.0
            if not math.isfinite(spread_value):
                return reject("invalid spread estimate (fail closed)", gross=gross,
                              fee=fee_value, threshold_amount=threshold_amount)

        # Slippage is charged on both entry and exit, matching ExecutionConfig.
        if slippage is not None:
            slip_value, error = explicit_cost(slippage, "slippage")
            if error:
                return reject(error, gross=gross, fee=fee_value,
                              spread=spread_value,
                              threshold_amount=threshold_amount)
        elif slippage_rate is None:
            slip_value = 0.0
        else:
            rate = _finite_number(slippage_rate)
            if rate is None or rate < 0.0:
                return reject("invalid slippage rate (fail closed)", gross=gross,
                              fee=fee_value, spread=spread_value,
                              threshold_amount=threshold_amount)
            if notional is None and rate != 0.0:
                return reject("missing notional for slippage estimate", gross=gross,
                              fee=fee_value, spread=spread_value,
                              threshold_amount=threshold_amount)
            slip_value = (notional or 0.0) * rate * 2.0
            if not math.isfinite(slip_value):
                return reject("invalid slippage estimate (fail closed)", gross=gross,
                              fee=fee_value, spread=spread_value,
                              threshold_amount=threshold_amount)

        # Small documented volatility/participation proxy. If requested, it
        # needs both a valid volatility value and a notional basis.
        if market_impact is not None:
            impact_value, error = explicit_cost(market_impact, "market impact")
            if error:
                return reject(error, gross=gross, fee=fee_value,
                              spread=spread_value, slip=slip_value,
                              threshold_amount=threshold_amount)
        elif volatility is None:
            impact_value = 0.0
        else:
            vol = _finite_number(volatility)
            if vol is None or vol < 0.0:
                return reject("invalid volatility for market-impact estimate",
                              gross=gross, fee=fee_value, spread=spread_value,
                              slip=slip_value,
                              threshold_amount=threshold_amount)
            if notional is None and vol != 0.0:
                return reject("missing notional for market-impact estimate",
                              gross=gross, fee=fee_value, spread=spread_value,
                              slip=slip_value,
                              threshold_amount=threshold_amount)
            impact_value = vol * 0.001 * (notional or 0.0)
            if holding_period is not None:
                period = _finite_number(holding_period)
                if period is None or period <= 0.0:
                    return reject("invalid holding period for market impact",
                                  gross=gross, fee=fee_value, spread=spread_value,
                                  slip=slip_value,
                                  threshold_amount=threshold_amount)
                impact_value /= math.sqrt(period)
            if not math.isfinite(impact_value):
                return reject("invalid market-impact estimate (fail closed)",
                              gross=gross, fee=fee_value, spread=spread_value,
                              slip=slip_value,
                              threshold_amount=threshold_amount)

        net = gross - fee_value - spread_value - slip_value - impact_value
        if not math.isfinite(net):
            return reject("net_expected invalid", gross=gross, fee=fee_value,
                          spread=spread_value, slip=slip_value, impact=impact_value,
                          threshold_amount=threshold_amount)

        passes = net > threshold_amount
        verdict = "PASS" if passes else "REJECT"
        reason = (
            f"NET_EDGE_{verdict}: gross_expected={gross:.6f} "
            f"fees={fee_value:.6f} spread={spread_value:.6f} "
            f"slippage={slip_value:.6f} market_impact={impact_value:.6f} "
            f"net_expected={net:.6f} threshold_rate={self.threshold:.6f} "
            f"threshold_amount={threshold_amount:.6f}"
        )
        if passes:
            logger.debug(reason)
        else:
            logger.info(reason)
        return NetEdgeResult(
            passes=passes,
            gross_expected=gross,
            fees=fee_value,
            spread_cost=spread_value,
            slippage=slip_value,
            market_impact=impact_value,
            net_expected=net,
            threshold=self.threshold,
            reason=reason,
            threshold_amount=threshold_amount,
        )
