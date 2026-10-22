"""Market registry and session engine.

Deterministic and offline: session state is computed from the market's
timezone, trading hours, weekend days and a holiday table. External feeds or
broker calendars may refine these later, but the registry never fabricates a
session state it cannot compute (spec section 143).

Timezone handling is explicit everywhere (spec section 144): naive timestamps
are never silently assumed to be UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class MarketDefinition:
    market_id: str
    display_name: str
    country: str
    currency: str
    timezone: str
    exchanges: tuple[str, ...]
    session_open: time          # local exchange time
    session_close: time         # local exchange time
    weekend_days: tuple[int, ...]  # Monday=0 .. Sunday=6
    holidays: tuple[str, ...]   # ISO dates the exchange is closed
    default_tick_size: float
    default_lot_size: int
    symbol_help: str
    data_source_label: str
    live_source_label: str
    broker_mapping: str
    notes: str = ""


_INDIA_HOLIDAYS_2026 = (
    # Representative NSE trading holidays; the readiness center labels the
    # calendar source so staleness is visible instead of silently assumed.
    "2026-01-26", "2026-02-17", "2026-03-04", "2026-03-31", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-08-15", "2026-09-15", "2026-10-02",
    "2026-10-20", "2026-11-09", "2026-12-25",
)

MARKETS: Mapping[str, MarketDefinition] = {
    "india": MarketDefinition(
        market_id="india", display_name="India (NSE/BSE)", country="IN",
        currency="INR", timezone="Asia/Kolkata", exchanges=("NSE", "BSE"),
        session_open=time(9, 15), session_close=time(15, 30),
        weekend_days=(5, 6), holidays=_INDIA_HOLIDAYS_2026,
        default_tick_size=0.05, default_lot_size=1,
        symbol_help="NSE_EQ|<ISIN> or trading symbol (e.g. RELIANCE)",
        data_source_label="Upstox historical/realtime (India)",
        live_source_label="Upstox (India)", broker_mapping="UPSTOX",
        notes="Static-IP enforcement applies to live order traffic.",
    ),
    "us": MarketDefinition(
        market_id="us", display_name="United States (NYSE/NASDAQ)", country="US",
        currency="USD", timezone="America/New_York", exchanges=("NYSE", "NASDAQ"),
        session_open=time(9, 30), session_close=time(16, 0),
        weekend_days=(5, 6),
        holidays=("2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
                  "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
                  "2026-11-26", "2026-12-25"),
        default_tick_size=0.01, default_lot_size=1,
        symbol_help="Exchange-listed ticker (e.g. AAPL)",
        data_source_label="Alpaca read-only US equity market data (paper fills)",
        live_source_label="No live broker configured", broker_mapping="NONE",
        notes="US market-data paper mode only; no US live execution is configured.",
    ),
    "japan": MarketDefinition(
        market_id="japan", display_name="Japan (TSE)", country="JP",
        currency="JPY", timezone="Asia/Tokyo", exchanges=("TSE",),
        session_open=time(9, 0), session_close=time(15, 0),
        weekend_days=(5, 6), holidays=("2026-01-01", "2026-01-12", "2026-02-11",
                                       "2026-04-29", "2026-05-04", "2026-11-03"),
        default_tick_size=1.0, default_lot_size=100,
        symbol_help="TSE ticker or security code",
        data_source_label="Recorded/replay (no live source configured)",
        live_source_label="No live broker configured", broker_mapping="NONE",
        notes="Midday break 11:30-12:30 local is not modelled; session state is approximate.",
    ),
    "uk": MarketDefinition(
        market_id="uk", display_name="United Kingdom (LSE)", country="GB",
        currency="GBP", timezone="Europe/London", exchanges=("LSE",),
        session_open=time(8, 0), session_close=time(16, 30),
        weekend_days=(5, 6), holidays=("2026-01-01", "2026-04-03", "2026-04-06",
                                       "2026-05-04", "2026-05-25", "2026-08-31",
                                       "2026-12-25", "2026-12-28"),
        default_tick_size=0.01, default_lot_size=1,
        symbol_help="LSE ticker (e.g. SHEL)",
        data_source_label="Recorded/replay (no live source configured)",
        live_source_label="No live broker configured", broker_mapping="NONE",
    ),
    "hongkong": MarketDefinition(
        market_id="hongkong", display_name="Hong Kong (HKEX)", country="HK",
        currency="HKD", timezone="Asia/Hong_Kong", exchanges=("HKEX",),
        session_open=time(9, 30), session_close=time(16, 0),
        weekend_days=(5, 6), holidays=("2026-01-01", "2026-02-17", "2026-04-03",
                                       "2026-05-01", "2026-07-01", "2026-10-01"),
        default_tick_size=0.01, default_lot_size=100,
        symbol_help="HKEX stock code (e.g. 0700)",
        data_source_label="Recorded/replay (no live source configured)",
        live_source_label="No live broker configured", broker_mapping="NONE",
        notes="Midday break 12:00-13:00 local is not modelled; session state is approximate.",
    ),
    "singapore": MarketDefinition(
        market_id="singapore", display_name="Singapore (SGX)", country="SG",
        currency="SGD", timezone="Asia/Singapore", exchanges=("SGX",),
        session_open=time(9, 0), session_close=time(17, 0),
        weekend_days=(5, 6), holidays=("2026-01-01", "2026-02-17", "2026-05-01",
                                       "2026-08-09", "2026-12-25"),
        default_tick_size=0.01, default_lot_size=100,
        symbol_help="SGX ticker or security code",
        data_source_label="Recorded/replay (no live source configured)",
        live_source_label="No live broker configured", broker_mapping="NONE",
        notes="Midday break 12:00-13:00 local is not modelled; session state is approximate.",
    ),
    "global": MarketDefinition(
        market_id="global", display_name="Global / cross-market context", country="--",
        currency="USD", timezone="UTC", exchanges=(),
        session_open=time(0, 0), session_close=time(23, 59),
        weekend_days=(), holidays=(),
        default_tick_size=0.01, default_lot_size=1,
        symbol_help="Context-only market: indices, FX and commodities references",
        data_source_label="Recorded/replay (no live source configured)",
        live_source_label="Execution not available for context market",
        broker_mapping="NONE",
        notes="Context market: analysis only. Execution is never routed here.",
    ),
}

MARKET_IDS = tuple(MARKETS.keys())


def get_market(market_id: str) -> MarketDefinition:
    key = (market_id or "").strip().lower()
    if key not in MARKETS:
        raise KeyError(f"Unknown market '{market_id}'. Known markets: {', '.join(MARKET_IDS)}")
    return MARKETS[key]


def list_markets() -> list[dict]:
    out = []
    for market in MARKETS.values():
        out.append({
            "market_id": market.market_id,
            "display_name": market.display_name,
            "country": market.country,
            "currency": market.currency,
            "timezone": market.timezone,
            "exchanges": list(market.exchanges),
            "broker_mapping": market.broker_mapping,
            "data_source_label": market.data_source_label,
            "live_source_label": market.live_source_label,
            "notes": market.notes,
        })
    return out


@dataclass(frozen=True)
class SessionState:
    market_id: str
    status: str                 # OPEN | PRE_OPEN | CLOSED | HOLIDAY | WEEKEND | ALWAYS
    local_time_iso: str
    reason: str
    next_transition_iso: str | None
    minutes_to_next_transition: int | None
    timezone: str

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "status": self.status,
            "local_time": self.local_time_iso,
            "reason": self.reason,
            "next_transition": self.next_transition_iso,
            "minutes_to_next_transition": self.minutes_to_next_transition,
            "timezone": self.timezone,
        }


_PRE_OPEN_MINUTES = 15


def market_session_state(market_id: str, now: datetime | None = None) -> SessionState:
    """Deterministic session state for a market at ``now`` (default: current).

    ``now`` may be naive (interpreted in the market's own timezone) or aware
    (converted). The returned state always carries the market-local time so a
    caller can never mistake which clock the answer is based on.
    """
    market = get_market(market_id)
    tz = ZoneInfo(market.timezone)
    if now is None:
        local = datetime.now(tz)
    elif now.tzinfo is None:
        local = now.replace(tzinfo=tz)
    else:
        local = now.astimezone(tz)

    iso = local.isoformat(timespec="seconds")
    if market.market_id == "global":
        return SessionState(market.market_id, "ALWAYS", iso,
                            "Context market: no session gating", None, None, market.timezone)

    if market.market_id == "us":
        from app.markets.us_calendar import USEquitiesCalendar

        # The US answer comes from the same calendar object the paper engine
        # enforces, so "OPEN" here can never disagree with an accepted bar.
        us_calendar = USEquitiesCalendar()
        is_holiday = (
            local.weekday() < 5 and not us_calendar.is_trading_day(local.date())
        )
        close_time = us_calendar.close_time(local.date())
    else:
        is_holiday = local.date().isoformat() in market.holidays
        close_time = market.session_close
    date_iso = local.date().isoformat()
    if local.weekday() in market.weekend_days:
        next_open = _next_open(market, local)
        return SessionState(market.market_id, "WEEKEND", iso,
                            "Market is closed for the weekend",
                            next_open.isoformat(timespec="seconds"),
                            _minutes(local, next_open), market.timezone)
    if is_holiday:
        next_open = _next_open(market, local)
        return SessionState(market.market_id, "HOLIDAY", iso,
                            f"{date_iso} is a listed market holiday",
                            next_open.isoformat(timespec="seconds"),
                            _minutes(local, next_open), market.timezone)

    open_at = local.replace(hour=market.session_open.hour, minute=market.session_open.minute,
                            second=0, microsecond=0)
    close_at = local.replace(hour=close_time.hour, minute=close_time.minute,
                             second=0, microsecond=0)
    pre_open_at = open_at - timedelta(minutes=_PRE_OPEN_MINUTES)

    if local < pre_open_at:
        return SessionState(market.market_id, "CLOSED", iso, "Before the pre-open window",
                            pre_open_at.isoformat(timespec="seconds"),
                            _minutes(local, pre_open_at), market.timezone)
    if local < open_at:
        return SessionState(market.market_id, "PRE_OPEN", iso, "Pre-open window",
                            open_at.isoformat(timespec="seconds"),
                            _minutes(local, open_at), market.timezone)
    if local < close_at:
        return SessionState(market.market_id, "OPEN", iso, "Regular trading session",
                            close_at.isoformat(timespec="seconds"),
                            _minutes(local, close_at), market.timezone)
    next_open = _next_open(market, local)
    return SessionState(market.market_id, "CLOSED", iso, "After the session close",
                        next_open.isoformat(timespec="seconds"),
                        _minutes(local, next_open), market.timezone)


def _minutes(local: datetime, target: datetime) -> int:
    return max(0, int((target - local).total_seconds() // 60))


def _next_open(market: MarketDefinition, local: datetime) -> datetime:
    if market.market_id == "us":
        from app.markets.us_calendar import USEquitiesCalendar

        calendar = USEquitiesCalendar()
        bounds = calendar.next_session_bounds(local)
        if bounds is not None:
            return bounds[0]
        return datetime.combine(  # pragma: no cover — defensive
            local.date() + timedelta(days=1), market.session_open, ZoneInfo(market.timezone)
        )
    candidate = (local + timedelta(days=1)).replace(
        hour=market.session_open.hour, minute=market.session_open.minute,
        second=0, microsecond=0)
    for _ in range(14):  # two weeks always covers weekends + holiday runs
        if (candidate.weekday() not in market.weekend_days
                and candidate.date().isoformat() not in market.holidays):
            return candidate
        candidate += timedelta(days=1)
    return candidate  # pragma: no cover — defensive


_IDENTITY_SEPARATORS = ("|", ":")


def parse_instrument_identity(raw: str, market_id: str) -> dict:
    """Parse and validate a market-scoped instrument identity string.

    Accepted shapes:
      * ``EXCHANGE|SYMBOL`` — explicit exchange (must belong to the market);
      * ``SYMBOL`` — bare symbol, valid only when the market defines exchanges;
      * bare identities for the ``global`` context market are rejected for
        execution purposes (context market never executes).

    Returns a dict with ``valid`` plus parsed fields or a ``reason``. The
    function never raises on user input; invalid identity is a data fact.
    """
    market = get_market(market_id)
    text = (raw or "").strip()
    if not text:
        return {"valid": False, "reason": "empty instrument identity"}
    if market.market_id == "global":
        return {"valid": False, "reason": "global context market is not executable"}
    if len(text) > 64:
        return {"valid": False, "reason": "instrument identity longer than 64 characters"}

    exchange = None
    segment = None
    symbol = text
    for sep in _IDENTITY_SEPARATORS:
        if sep in text:
            exchange, symbol = (part.strip() for part in text.split(sep, 1))
            break
    if not symbol or any(ch.isspace() for ch in symbol):
        return {"valid": False, "reason": "symbol part missing or contains whitespace"}
    if exchange is not None:
        # Upstox-style segment suffixes (NSE_EQ, NSE_FO, BSE_EQ...) map onto
        # the exchange part; the segment is preserved, never guessed.
        parts = exchange.upper().split("_", 1)
        base = parts[0]
        if len(parts) == 2:
            segment = parts[1]
        if market.exchanges and base not in market.exchanges:
            return {"valid": False,
                    "reason": f"exchange '{exchange}' does not belong to market "
                              f"'{market.market_id}' ({', '.join(market.exchanges)})"}
        exchange = base
    elif market.exchanges:
        exchange = market.exchanges[0]
    return {"valid": True, "market_id": market.market_id, "exchange": exchange,
            "segment": segment, "symbol": symbol,
            "identity": f"{exchange}|{symbol}" if exchange else symbol}
