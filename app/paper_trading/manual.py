"""Manual paper orders — the "one click, still simulated" path.

Everything here acts on :class:`~app.paper_trading.broker.PaperBroker`, the
in-process simulator. There is no broker client in this module and no code path
that can reach an exchange, so a manual order is exactly as real as a strategy
order: a row in the local paper account.

Two order shapes are supported, and the difference matters:

* ``MARKET`` fills immediately at the **last completed bar's close** plus the
  configured commission, half-spread and slippage. The forming bar is never
  used, because its price is not known yet — using it would be look-ahead.
  The response states the bar time the price came from.
* ``LIMIT`` rests until a bar trades through it and is filled by the normal
  ``on_bar`` path, exactly like a strategy order. A marketable limit (already
  through the last close) fills immediately at the close-based price.

Risk limits are the same configured caps a strategy order is subject to, plus
two invariants that are not configurable: cash is never overspent and shorting
is never allowed (borrow availability and costs are not modelled anywhere in
this project, so a short could not be defended).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from app.backtesting.orders import ACTIVE_STATUSES, Order
from app.data.models import parse_timestamp, validate_symbol


# Proportional slack for money comparisons: positions are floats, so an exact
# equality test would reject an order sized to the last cent of its cap.
_EPS = 1e-6


logger = logging.getLogger("bot.manual")


class ManualOrderRejected(ValueError):
    """A manual order that the paper account will not accept, with a reason."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ManualLimits:
    """The configured hard caps, mirrored from Settings (0 = disabled)."""

    max_position_value: float = 0.0
    max_open_positions: int = 0
    max_daily_loss: float = 0.0
    max_trades_per_day: int = 0

    @classmethod
    def from_settings(cls, settings) -> "ManualLimits":
        return cls(
            max_position_value=float(getattr(settings, "max_position_value", 0.0) or 0.0),
            max_open_positions=int(getattr(settings, "max_open_positions", 0) or 0),
            max_daily_loss=float(getattr(settings, "max_daily_loss", 0.0) or 0.0),
            max_trades_per_day=int(getattr(settings, "max_trades_per_day", 0) or 0),
        )

    def to_dict(self) -> dict:
        return {
            "max_position_value": self.max_position_value,
            "max_open_positions": self.max_open_positions,
            "max_daily_loss": self.max_daily_loss,
            "max_trades_per_day": self.max_trades_per_day,
            "allow_short": False,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def account_clock(broker) -> datetime:
    """The instant a manual order is stamped with.

    The paper account advances on bars, so an order belongs to the account's
    clock, not to the wall clock. Stamping it with wall-clock "now" would put a
    limit order permanently in the future of every historical bar: it could
    never fill, and its marker could never be drawn. Using the last processed
    bar means the order fills on the *next* bar through the ordinary
    ``on_bar`` path, which is exactly how a strategy order behaves — and it
    cannot fill on the bar it was placed against.
    """
    stamp = getattr(broker.portfolio, "last_timestamp", None)
    if stamp:
        try:
            return parse_timestamp(stamp)
        except ValueError as exc:
            # A corrupt cursor must be visible, not silently replaced.
            logger.warning("account_clock_unreadable", extra={"reason": type(exc).__name__})
    return _now()


def last_price(broker, symbol: str) -> tuple[float, str] | None:
    """(close, bar timestamp) of the newest completed bar for `symbol`."""
    last = getattr(broker, "last_candles", {}).get(symbol)
    if last:
        try:
            close = float(last["close"])
            stamp = str(last["timestamp"])
            if close > 0:
                parse_timestamp(stamp)
                return close, stamp
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "last_candle_unusable",
                extra={"symbol": symbol, "reason": type(exc).__name__},
            )
    mark = getattr(broker.portfolio, "marks", {}).get(symbol)
    if mark and mark > 0:
        return float(mark), broker.portfolio.last_timestamp or ""
    return None


def _entries_today(broker) -> int:
    """BUY fills stamped on the current UTC date.

    A regular cash-equity session (13:30-20:00 UTC for the US, 03:45-10:00 for
    India) never crosses a UTC midnight, so the UTC date is the trading day for
    every session this project supports.
    """
    today = _now().date().isoformat()
    return sum(
        1
        for fill in getattr(broker, "fills", [])
        if fill.side == "BUY" and str(fill.timestamp)[:10] == today
    )


def _account(broker) -> dict:
    return broker.get_balance()


def _pending_buy_notional(broker, symbol: str, price: float) -> float:
    return sum(
        order.remaining * (order.limit_price or price)
        for order in broker.orders.values()
        if order.symbol == symbol
        and order.side == "BUY"
        and order.status in ACTIVE_STATUSES
    )


def _held(broker, symbol: str):
    return broker.portfolio.positions.get(symbol)


def ticket(broker, symbol: str, limits: ManualLimits) -> dict:
    """What the trade panel should show: price, headroom and blockers."""
    validate_symbol(symbol)
    price_info = last_price(broker, symbol)
    account = _account(broker)
    position = _held(broker, symbol)
    held_value = 0.0
    if position is not None:
        held_value = position.quantity * (price_info[0] if price_info else position.entry_price)

    blocked = None
    if limits.max_daily_loss and account["daily_pnl"] <= -limits.max_daily_loss:
        blocked = (
            f"daily loss limit reached ({account['daily_pnl']:.2f}); "
            "new entries are blocked for the rest of the day"
        )
    elif limits.max_trades_per_day and _entries_today(broker) >= limits.max_trades_per_day:
        blocked = (
            f"daily entry limit reached ({limits.max_trades_per_day}); "
            "exits are still allowed"
        )

    max_buy_quantity = 0.0
    if price_info:
        price = broker.config.fill_price(price_info[0], "BUY")
        cash_bound = account["cash"] / (price * (1 + broker.config.commission))
        cap_bound = cash_bound
        if limits.max_position_value:
            headroom = max(0.0, limits.max_position_value - held_value)
            cap_bound = headroom / (price * (1 + broker.config.commission))
        # Floored, never rounded up: the number the ticket shows must be
        # acceptable to the same checks the order will face.
        max_buy_quantity = max(0.0, math.floor(min(cash_bound, cap_bound) * 1e6) / 1e6)
        if blocked:
            max_buy_quantity = 0.0

    return {
        "symbol": symbol,
        "last_price": None if price_info is None else round(price_info[0], 6),
        "priced_at": None if price_info is None else price_info[1],
        "price_source": "last_closed_bar" if price_info else "none",
        "cash": round(account["cash"], 6),
        "equity": round(account["equity"], 6),
        "daily_pnl": round(account["daily_pnl"], 6),
        "position": None
        if position is None
        else {
            "quantity": round(position.quantity, 8),
            "entry_price": round(position.entry_price, 6),
            "value": round(held_value, 6),
        },
        "max_buy_quantity": round(max_buy_quantity, 8),
        "max_sell_quantity": 0.0 if position is None else round(position.quantity, 8),
        "blocked": blocked,
        "limits": limits.to_dict(),
        "entries_today": _entries_today(broker),
        "open_positions": len(broker.portfolio.positions),
        "paper_only": True,
        "live_trading": False,
    }


def _check_buy(broker, symbol, quantity, price, limits) -> None:
    """Configurable caps first, then the invariants that cannot be configured.

    Order matters for the message an operator sees: telling someone "not enough
    cash" when the position cap is what actually binds is misleading, and the
    cap is the thing they are expected to respect.
    """
    cost = quantity * price * (1 + broker.config.commission)
    account = _account(broker)
    if limits.max_daily_loss and account["daily_pnl"] <= -limits.max_daily_loss:
        raise ManualOrderRejected(
            "max_daily_loss",
            f"daily loss limit reached ({account['daily_pnl']:,.2f}); "
            "new entries are blocked for the rest of the day",
        )
    if limits.max_trades_per_day and _entries_today(broker) >= limits.max_trades_per_day:
        raise ManualOrderRejected(
            "max_trades_per_day",
            f"daily entry limit reached ({limits.max_trades_per_day})",
        )
    position = _held(broker, symbol)
    active_buys = [
        order
        for order in broker.orders.values()
        if order.symbol == symbol and order.side == "BUY" and order.status in ACTIVE_STATUSES
    ]
    if limits.max_open_positions and position is None and not active_buys:
        if len(broker.portfolio.positions) >= limits.max_open_positions:
            raise ManualOrderRejected(
                "max_open_positions",
                f"{limits.max_open_positions} positions are already open",
            )
    if limits.max_position_value:
        held = 0.0 if position is None else position.quantity * price
        pending = _pending_buy_notional(broker, symbol, price)
        if held + pending + cost > limits.max_position_value + _EPS:
            raise ManualOrderRejected(
                "max_position_value",
                f"position cap {limits.max_position_value:,.2f} would be exceeded "
                f"(holding {held:,.2f}, pending {pending:,.2f}, this order {cost:,.2f})",
            )
    if cost > account["cash"] + 1e-9:
        raise ManualOrderRejected(
            "insufficient_cash",
            f"needs {cost:,.2f} of {account['currency']} cash, "
            f"{account['cash']:,.2f} available",
        )


def _check_sell(broker, symbol, quantity) -> None:
    position = _held(broker, symbol)
    if position is None:
        raise ManualOrderRejected(
            "insufficient_position",
            "no position to sell: shorting is not modelled in paper trading",
        )
    reserved = sum(
        order.remaining
        for order in broker.orders.values()
        if order.symbol == symbol and order.side == "SELL" and order.status in ACTIVE_STATUSES
    )
    if quantity > position.quantity - reserved + 1e-9:
        raise ManualOrderRejected(
            "insufficient_position",
            f"holding {position.quantity:g}, {reserved:g} already reserved by working "
            "sell orders",
        )


def place_order(
    broker,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    notional: float | None = None,
    order_type: str = "MARKET",
    limit_price: float | None = None,
    limits: ManualLimits | None = None,
    timestamp: datetime | None = None,
) -> dict:
    """Place one simulated manual order. Returns the order, fill and account."""
    limits = limits or ManualLimits()
    validate_symbol(symbol)
    side = str(side).upper()
    order_type = str(order_type or "MARKET").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if order_type not in {"MARKET", "LIMIT"}:
        raise ValueError("order_type must be MARKET or LIMIT")
    if (quantity is None) == (notional is None):
        raise ValueError("supply exactly one of quantity or notional")
    if order_type == "LIMIT" and (limit_price is None or float(limit_price) <= 0):
        raise ValueError("a LIMIT order needs a positive limit_price")

    price_info = last_price(broker, symbol)
    if price_info is None:
        raise ManualOrderRejected(
            "no_market_price",
            f"no completed bar for {symbol} yet: start a paper session or pick a "
            "symbol that has data",
        )
    reference, priced_at = price_info
    fill_price = broker.config.fill_price(reference, side)

    if quantity is None:
        notional = float(notional)
        if notional <= 0:
            raise ValueError("notional must be positive")
        quantity = notional / (fill_price * (1 + broker.config.commission))
    quantity = float(quantity)
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    if side == "BUY":
        _check_buy(broker, symbol, quantity, fill_price, limits)
    else:
        _check_sell(broker, symbol, quantity)

    placed_at = (timestamp or account_clock(broker)).astimezone(timezone.utc)
    order = Order(
        symbol,
        side,
        quantity,
        placed_at.isoformat(),
        "manual",
        order_type,
        None if limit_price is None else float(limit_price),
        reason="manual",
    )
    broker.orders[order.id] = order

    filled_now = False
    if order_type == "MARKET":
        filled_now = True
    elif limit_price is not None:
        # A marketable limit behaves like a market order, never worse than the
        # limit: BUY pays at most the limit, SELL receives at least it.
        if side == "BUY" and limit_price >= fill_price:
            filled_now = True
        elif side == "SELL" and limit_price <= fill_price:
            filled_now = True

    fill = None
    if filled_now:
        price = fill_price
        if order_type == "LIMIT":
            price = min(fill_price, limit_price) if side == "BUY" else max(
                fill_price, limit_price
            )
        broker._fill(order, quantity, price, placed_at.isoformat(), "manual", reference)
        fill = broker.fills[-1]
        snapshot = broker.portfolio.snapshot(placed_at.isoformat(), broker.currency)
        broker._persist(snapshot)
    else:
        broker._persist()

    return {
        "order": order.to_dict(),
        "fill": None
        if fill is None
        else {
            "id": fill.id,
            "timestamp": fill.timestamp,
            "price": round(fill.price, 6),
            "quantity": round(fill.quantity, 8),
            "fee": round(fill.fee, 6),
            "reason": fill.reason,
        },
        "status": order.status,
        "simulated": True,
        "paper_only": True,
        "live_trading": False,
        "priced_at": priced_at,
        "stamped_at": placed_at.isoformat(),
        "requested_at": _now().isoformat(),
        "account": {
            key: (round(value, 6) if isinstance(value, float) else value)
            for key, value in broker.get_balance().items()
            if key != "positions"
        },
        "ticket": ticket(broker, symbol, limits),
    }
