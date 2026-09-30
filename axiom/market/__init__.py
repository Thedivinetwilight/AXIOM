"""AXIOM market data layer: contract, validation, calendars, closed-candle gate."""

from axiom.market.calendar import (
    ContinuousCalendar,
    DailyCalendar,
    SessionCalendar,
    TradingCalendar,
)
from axiom.market.gate import ClosedBarWindow, ClosedCandleGate
from axiom.market.http import HttpJsonMarketDataProvider
from axiom.market.provider import (
    MarketDataBar,
    MarketDataError,
    MarketDataProvider,
    MarketDataResult,
)
from axiom.market.replay import ReplayMarketDataProvider
from axiom.market.validation import require_valid, validate_market_data

__all__ = [
    "ClosedBarWindow",
    "ClosedCandleGate",
    "ContinuousCalendar",
    "DailyCalendar",
    "HttpJsonMarketDataProvider",
    "MarketDataBar",
    "MarketDataError",
    "MarketDataProvider",
    "MarketDataResult",
    "ReplayMarketDataProvider",
    "SessionCalendar",
    "TradingCalendar",
    "require_valid",
    "validate_market_data",
]
