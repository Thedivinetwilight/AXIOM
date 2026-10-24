"""Trade markers and position levels drawn on the chart.

Every marker comes from a fill that the local paper broker actually recorded.
There is no marker source other than `PaperBroker.fills`, so the chart cannot
show a trade the account did not make.
"""

from __future__ import annotations

from app.charting.series import bucket_index
from app.data.models import Candle


def trade_markers(
    broker,
    symbol: str,
    candles: list[Candle],
    timeframe: str,
    *,
    limit: int = 300,
) -> list[dict]:
    """Fills for `symbol` binned onto the drawn bars, oldest first."""
    fills = [f for f in getattr(broker, "fills", []) if f.symbol == symbol]
    markers: list[dict] = []
    last_bar = candles[-1].timestamp if candles else None
    for fill in fills[-limit:]:
        index = bucket_index(candles, fill.timestamp, timeframe)
        beyond = False
        if index is None and last_bar is not None:
            # A fill that happened after the newest drawn bar belongs to the
            # newest bar on screen, exactly as a live order appears at the
            # right-hand edge of a chart before the next candle exists. It is
            # flagged so the tooltip can say the bar is not the fill's own.
            try:
                from app.data.models import parse_timestamp

                if parse_timestamp(fill.timestamp) > last_bar:
                    index, beyond = len(candles) - 1, True
            except ValueError:
                index = None
        markers.append(
            {
                "bar": index,
                "beyond_last_bar": beyond,
                "timestamp": fill.timestamp,
                "side": fill.side,
                "price": round(fill.price, 6),
                "quantity": round(fill.quantity, 8),
                "fee": round(fill.fee, 6),
                "reason": fill.reason,
                "strategy": fill.strategy,
                "order_id": fill.order_id,
                # A fill outside the drawn window still exists; the page shows
                # it in the trade list instead of pretending it never happened.
                "on_chart": index is not None,
            }
        )
    return markers


def order_markers(broker, symbol: str, candles: list[Candle], timeframe: str) -> list[dict]:
    """Still-working simulated orders, so a resting limit is visible on the chart."""
    from app.backtesting.orders import ACTIVE_STATUSES

    markers: list[dict] = []
    last_bar = candles[-1].timestamp if candles else None
    for order in getattr(broker, "orders", {}).values():
        if order.symbol != symbol or order.status not in ACTIVE_STATUSES:
            continue
        index = bucket_index(candles, order.timestamp, timeframe)
        if index is None and last_bar is not None:
            # A resting order placed after the newest drawn bar is still a
            # live instruction: draw it at the right-hand edge rather than
            # hiding it, exactly as a live chart shows a working order.
            try:
                from app.data.models import parse_timestamp

                if parse_timestamp(order.timestamp) > last_bar:
                    index = len(candles) - 1
            except ValueError:
                index = None
        markers.append(
            {
                "bar": index,
                "timestamp": order.timestamp,
                "side": order.side,
                "quantity": round(order.remaining, 8),
                "limit_price": order.limit_price,
                "status": order.status,
                "order_type": order.order_type,
                "order_id": order.id,
                "on_chart": index is not None,
            }
        )
    return markers


def position_levels(broker, symbol: str) -> dict | None:
    """Average entry plus the protective levels the broker would enforce."""
    position = getattr(broker, "portfolio", None)
    if position is None:
        return None
    held = position.positions.get(symbol)
    if held is None:
        return None
    config = broker.config
    stop = held.entry_price * (1 - config.stop_loss) if config.stop_loss else None
    target = held.entry_price * (1 + config.take_profit) if config.take_profit else None
    mark = position.marks.get(symbol, held.entry_price)
    return {
        "quantity": round(held.quantity, 8),
        "entry_price": round(held.entry_price, 6),
        "mark_price": round(mark, 6),
        "entry_fees": round(held.entry_fees, 6),
        "entry_timestamp": held.entry_timestamp,
        "strategy": held.strategy,
        "stop_price": None if stop is None else round(stop, 6),
        "target_price": None if target is None else round(target, 6),
        "unrealized_pnl": round(
            held.quantity * (mark - held.entry_price) - held.entry_fees, 6
        ),
    }
