"""Candle fill model and explicit exchange-session extension point."""

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo
from app.backtesting.costs import ExecutionConfig
from app.backtesting.orders import Order
from app.data.models import Candle


class ContinuousCalendar:
    def is_open(self, timestamp: datetime) -> bool:
        return True


@dataclass
class SessionCalendar:
    timezone: str = "Asia/Kolkata"
    open_time: time = time(9, 15)
    close_time: time = time(15, 30)
    holidays: set[str] = field(default_factory=set)

    def is_open(self, timestamp: datetime) -> bool:
        local = timestamp.astimezone(ZoneInfo(self.timezone))
        return (
            local.weekday() < 5
            and local.date().isoformat() not in self.holidays
            and self.open_time <= local.time() < self.close_time
        )


def execution_price(order: Order, candle: Candle, config: ExecutionConfig) -> float | None:
    reference = candle.open
    if order.order_type == "LIMIT":
        limit = order.limit_price
        if order.side == "BUY":
            if candle.low > limit:
                return None
            reference = min(candle.open, limit)
        else:
            if candle.high < limit:
                return None
            reference = max(candle.open, limit)
    price = config.fill_price(reference, order.side)
    if order.order_type == "LIMIT":
        price = (
            min(price, order.limit_price) if order.side == "BUY" else max(price, order.limit_price)
        )
    return price


def make_calendar(name: str, timeframe: str):
    if name == "continuous":
        return ContinuousCalendar()
    if name == "nse-session" and timeframe != "1D":
        return SessionCalendar()
    raise ValueError(
        "NSE session example supports intraday only; add daily/holiday calendar adapter"
    )


def calendar_signature(calendar) -> dict:
    if isinstance(calendar, SessionCalendar):
        return {
            "type": "session",
            "timezone": calendar.timezone,
            "open": calendar.open_time.isoformat(),
            "close": calendar.close_time.isoformat(),
            "holidays": sorted(calendar.holidays),
        }
    return {"type": type(calendar).__name__}
