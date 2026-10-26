"""Contract normalization and server-side pre-trade risk decisions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

from app.vantage.models import (
    AccountSnapshot, Contract, OrderInput, Quote, RiskLimits, utcnow,
)


class ContractViolation(ValueError):
    pass


def _decimal(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractViolation(f"{label} is not a valid decimal") from exc
    if not result.is_finite():
        raise ContractViolation(f"{label} must be finite")
    return result


def _step_aligned(value: Decimal, minimum: Decimal, step: Decimal) -> bool:
    try:
        units = (value - minimum) / step
    except (InvalidOperation, ZeroDivisionError):
        return False
    return units == units.to_integral_value()


def normalize_order(order: OrderInput, contract: Contract, quote: Quote) -> OrderInput:
    """Validate lot/tick/stop rules exactly; never silently round an order."""
    order.validate()
    if order.symbol != contract.symbol:
        raise ContractViolation("Order symbol does not match the reviewed contract")
    if not contract.metadata_complete or not contract.tradable:
        raise ContractViolation("Instrument metadata is incomplete or the product is not understood")
    minimum = _decimal(contract.volume_min, "minimum volume")
    maximum = _decimal(contract.volume_max, "maximum volume")
    step = _decimal(contract.volume_step, "volume step")
    volume = _decimal(order.volume, "volume")
    if volume < minimum or volume > maximum:
        raise ContractViolation(f"Volume must be between {minimum} and {maximum} lots")
    if not _step_aligned(volume, minimum, step):
        raise ContractViolation(f"Volume must be {minimum} plus a whole number of {step} lot steps")
    # Formatting through the step exponent avoids binary-float artifacts in the
    # outgoing MT5 request while preserving the operator's exact amount.
    normalized_volume = float(volume)
    tick = _decimal(contract.tick_size, "tick size")

    def normalize_price(value, label):
        if value is None:
            return None
        price = _decimal(value, label)
        units = price / tick
        if units != units.to_integral_value():
            raise ContractViolation(f"{label} must align to the {tick} tick size")
        digits = contract.digits if contract.digits is not None else 8
        return float(f"{price:.{digits}f}")

    limit_price = normalize_price(order.limit_price, "limit_price")
    stop_loss = normalize_price(order.stop_loss, "stop_loss")
    take_profit = normalize_price(order.take_profit, "take_profit")
    if order.order_type == "MARKET" and not contract.supports_market:
        raise ContractViolation("The broker symbol does not support market orders")
    if order.order_type == "LIMIT" and not contract.supports_limit:
        raise ContractViolation("The broker symbol does not support limit orders")
    if stop_loss is not None and not contract.supports_stop_loss:
        raise ContractViolation("The broker symbol does not support stop-loss on entry")
    if take_profit is not None and not contract.supports_take_profit:
        raise ContractViolation("The broker symbol does not support take-profit on entry")
    if order.order_type == "LIMIT":
        if order.side == "BUY" and limit_price >= quote.ask:
            raise ContractViolation("A BUY LIMIT price must be below the current ask")
        if order.side == "SELL" and limit_price <= quote.bid:
            raise ContractViolation("A SELL LIMIT price must be above the current bid")
    reference = limit_price if limit_price is not None else (
        quote.ask if order.side == "BUY" else quote.bid
    )
    if order.side == "BUY":
        if stop_loss is not None and stop_loss >= reference:
            raise ContractViolation("BUY stop-loss must be below the entry reference")
        if take_profit is not None and take_profit <= reference:
            raise ContractViolation("BUY take-profit must be above the entry reference")
    else:
        if stop_loss is not None and stop_loss <= reference:
            raise ContractViolation("SELL stop-loss must be above the entry reference")
        if take_profit is not None and take_profit >= reference:
            raise ContractViolation("SELL take-profit must be below the entry reference")
    point = _decimal(contract.point, "point")
    minimum_distance = float(point * (contract.stops_level_points or 0))
    for label, value in (("stop_loss", stop_loss), ("take_profit", take_profit)):
        if value is not None and minimum_distance > 0:
            market = quote.bid if label == "stop_loss" and order.side == "BUY" else (
                quote.ask if label == "stop_loss" else quote.ask if order.side == "BUY" else quote.bid
            )
            if abs(market - value) + 1e-12 < minimum_distance:
                raise ContractViolation(f"{label} violates the broker's {minimum_distance} stop distance")
    return replace(
        order, volume=normalized_volume, limit_price=limit_price,
        stop_loss=stop_loss, take_profit=take_profit,
    )


def evaluate_entry(
    order: OrderInput,
    contract: Contract,
    quote: Quote,
    account: AccountSnapshot,
    limits: RiskLimits,
    *,
    positions: list[dict],
    working_orders: list[dict],
    current_margin_exposure: float,
    estimated_margin: float,
    trades_today: int,
    day_start_equity: float,
    peak_equity: float,
    now: datetime | None = None,
) -> dict:
    """Return every failed check. Callers must refuse if ``allowed`` is false."""
    now = now or utcnow()
    issues = list(limits.issues())
    if not account.trading_permitted:
        issues.append("account or terminal trading permission is disabled")
    if account.equity <= 0 or account.margin_free < 0:
        issues.append("account funds/margin state is invalid")
    age = quote.age_seconds(now)
    if age < 0:
        issues.append("quote timestamp is in the future")
    elif limits.max_quote_age_seconds > 0 and age > limits.max_quote_age_seconds:
        issues.append(f"quote is stale ({age:.3f}s old)")
    if limits.max_spread_bps > 0 and quote.spread_bps > limits.max_spread_bps:
        issues.append(f"spread {quote.spread_bps:.2f}bps exceeds the configured cap")
    if contract.trade_mode == 0:
        issues.append("symbol trading is disabled")
    elif contract.trade_mode == 3:
        issues.append("symbol is close-only; use a position-ticket close action")
    elif contract.trade_mode == 1 and order.side != "BUY":
        issues.append("symbol is long-only")
    elif contract.trade_mode == 2 and order.side != "SELL":
        issues.append("symbol is short-only")
    if limits.max_order_volume > 0 and order.volume > limits.max_order_volume + 1e-12:
        issues.append("order volume exceeds max_order_volume")
    opposite_positions = [
        item for item in positions if item.get("symbol") == order.symbol
        and item.get("side") not in {order.side, "UNKNOWN"}
    ]
    if opposite_positions:
        issues.append("opposite broker position exists; use its position-ticket close action, not an implicit reversal")
    same_side_positions = [
        item for item in positions if item.get("symbol") == order.symbol
        and item.get("side") == order.side
    ]
    same_side_orders = [
        item for item in working_orders if item.get("symbol") == order.symbol
        and item.get("side") == order.side
    ]
    symbol_volume = sum(float(item.get("volume") or 0) for item in same_side_positions)
    symbol_volume += sum(float(item.get("volume_current") or 0) for item in same_side_orders)
    if limits.max_symbol_volume > 0 and symbol_volume + order.volume > limits.max_symbol_volume + 1e-12:
        issues.append("aggregate symbol volume exceeds max_symbol_volume")
    if contract.volume_limit and contract.volume_limit > 0 and (
        symbol_volume + order.volume > contract.volume_limit + 1e-12
    ):
        issues.append("broker directional volume_limit would be exceeded")
    if estimated_margin < 0 or not isfinite(estimated_margin):
        issues.append("margin estimate is invalid")
    if limits.max_order_margin > 0 and estimated_margin > limits.max_order_margin + 1e-9:
        issues.append("estimated order margin exceeds max_order_margin")
    projected_margin = current_margin_exposure + estimated_margin
    if limits.max_total_margin > 0 and projected_margin > limits.max_total_margin + 1e-9:
        issues.append("aggregate account-currency margin exposure exceeds max_total_margin")
    if estimated_margin > account.margin_free + 1e-9:
        issues.append("insufficient free margin for the MT5 estimate")
    if account.equity > 0 and limits.max_margin_usage_pct > 0:
        projected_usage = projected_margin / account.equity * 100
        if projected_usage > limits.max_margin_usage_pct + 1e-9:
            issues.append("projected margin usage exceeds max_margin_usage_pct")
    # Conservatively count every entry at the cap. Netting could merge some
    # same-symbol entries, but assuming that without explicit account-mode
    # semantics could undercount hedged positions.
    if limits.max_open_positions > 0 and len(positions) >= limits.max_open_positions:
        issues.append("maximum open positions reached")
    if day_start_equity <= 0 or peak_equity <= 0:
        issues.append("daily/peak equity baseline is unavailable")
    else:
        daily_loss = max(0.0, day_start_equity - account.equity)
        drawdown = max(0.0, (peak_equity - account.equity) / peak_equity * 100)
        if limits.max_daily_loss > 0 and daily_loss >= limits.max_daily_loss:
            issues.append("maximum daily loss reached")
        if limits.max_drawdown_pct > 0 and drawdown >= limits.max_drawdown_pct:
            issues.append("maximum drawdown reached")
    if limits.max_trades_per_day > 0 and trades_today >= limits.max_trades_per_day:
        issues.append("maximum daily order-attempt count reached")
    return {
        "allowed": not issues,
        "issues": issues,
        "quote_age_seconds": age,
        "spread_bps": quote.spread_bps,
        "estimated_price": quote.ask if order.side == "BUY" else quote.bid,
        "estimated_margin": estimated_margin,
        "current_margin_exposure": current_margin_exposure,
        "projected_margin_exposure": projected_margin,
        "symbol_volume_after": symbol_volume + order.volume,
        "trades_today": trades_today,
        "commission_estimate": None,
        "cost_note": "Commission/financing is not estimated unless MT5 supplies it; deals remain authoritative.",
    }
