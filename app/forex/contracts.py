"""Fail-closed contract identity, quote validation, and paper accounting.

Candidate research scope is exactly one India exchange-traded currency future:
NSE USDINR. It is not OTC spot FX and not a CFD. No order API is imported or
called here. Contract terms are checked against an Upstox instrument-master
record at runtime; this module does not invent a current expiry or instrument
key.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

MVP_PAIR = "USDINR"
JURISDICTION = "IN"
EXCHANGE = "NSE"
EXCHANGE_SEGMENT = "NCD_FO"  # Upstox provider segment for currency derivatives
PRODUCT = "EXCHANGE_TRADED_CURRENCY_FUTURE"
CONTRACT_UNITS = 1000  # USD per standard NSE USDINR futures lot
TICK_SIZE = Decimal("0.0025")  # INR per USD
PIP_SIZE = Decimal("0.01")  # display convention only; not an OTC spot pip
MARKET_TZ = "Asia/Kolkata"
SESSION_OPEN = "09:00"
SESSION_CLOSE = "17:00"
QUOTE_MAX_AGE_SECONDS = 5.0
_KEY = re.compile(r"^NCD_FO\|[0-9]{1,20}$")


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{name} must be a finite positive decimal") from None
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be a finite positive decimal")
    return result


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError("expiry must be an ISO date (YYYY-MM-DD)") from None


def _aligned(value: Decimal, step: Decimal) -> bool:
    return value % step == 0


@dataclass(frozen=True)
class ForexContract:
    """Canonical identity plus a provider-specific key for one expiry."""

    pair: str
    jurisdiction: str
    exchange: str
    segment: str
    product: str
    expiry: date
    provider: str
    provider_instrument_key: str
    trading_symbol: str
    units_per_lot: int
    tick_size: Decimal
    pip_size: Decimal
    provider_lot_size: int
    provider_tick_size: Decimal

    @property
    def identity(self) -> str:
        # Pair/venue/product/expiry are canonical. The provider key remains a
        # distinct mapping and does not contaminate the market identity.
        return (f"{self.jurisdiction}:{self.exchange}:CDS:FUT:"
                f"{self.pair}:{self.expiry.isoformat()}")

    @property
    def provider_format(self) -> str:
        return self.provider_instrument_key

    @property
    def ticks_per_pip(self) -> int:
        return int(self.pip_size / self.tick_size)

    def price(self, value: Any) -> Decimal:
        result = _decimal(value, "price")
        if not _aligned(result, self.tick_size):
            raise ValueError(f"price must align to exchange tick {self.tick_size}")
        return result

    def pip_value_inr(self, lots: int = 1) -> Decimal:
        _validate_lots(lots)
        return self.pip_size * self.units_per_lot * lots

    def tick_value_inr(self, lots: int = 1) -> Decimal:
        _validate_lots(lots)
        return self.tick_size * self.units_per_lot * lots

    def notional_inr(self, price: Any, lots: int = 1) -> Decimal:
        _validate_lots(lots)
        return self.price(price) * self.units_per_lot * lots

    def pnl_inr(self, side: str, entry: Any, exit: Any, lots: int = 1) -> Decimal:
        _validate_lots(lots)
        if side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        sign = Decimal(1) if side == "LONG" else Decimal(-1)
        return (self.price(exit) - self.price(entry)) * self.units_per_lot * lots * sign


def _validate_lots(lots: int) -> None:
    if type(lots) is not int or lots <= 0:
        raise ValueError("lots must be a positive integer")


def contract_from_upstox_record(record: dict, *, as_of: date | None = None) -> ForexContract:
    """Accept only an exact current-contract row with verified scope/terms.

    This is a parser for the documented Upstox instrument-master/search shape;
    it does not fetch data or prove the account has access to it.
    """
    if not isinstance(record, dict):
        raise ValueError("instrument record must be an object")
    key = str(record.get("instrument_key", ""))
    if not _KEY.fullmatch(key):
        raise ValueError("instrument_key must use the Upstox NCD_FO|<numeric-token> format")
    if str(record.get("segment", "")).upper() != EXCHANGE_SEGMENT:
        raise ValueError("instrument segment must be NCD_FO")
    if str(record.get("exchange", "")).upper() != EXCHANGE:
        raise ValueError("instrument exchange must be NSE")
    if str(record.get("instrument_type", "")).upper() != "FUT":
        raise ValueError("only exchange-traded futures are in this paper scope")
    if str(record.get("underlying_type", "")).upper() != "CUR":
        raise ValueError("underlying_type must be CUR; equity/CFD/spot instruments are rejected")
    if re.sub(r"[^A-Z]", "", str(record.get("underlying_symbol", "")).upper()) != MVP_PAIR:
        raise ValueError("only the USDINR underlying is in this paper scope")
    raw_lot_size = record.get("lot_size")
    try:
        if isinstance(raw_lot_size, bool) or Decimal(str(raw_lot_size)) != CONTRACT_UNITS:
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(
            f"provider lot_size must match the published {CONTRACT_UNITS} USD contract"
        ) from None
    lot_size = CONTRACT_UNITS
    tick = _decimal(record.get("tick_size"), "provider tick_size")
    if tick != TICK_SIZE:
        raise ValueError(f"provider tick_size must match the published INR tick {TICK_SIZE}")
    expiry = _date(record.get("expiry"))
    today = as_of or datetime.now(ZoneInfo(MARKET_TZ)).date()
    if expiry <= today:
        raise ValueError("expired contracts cannot be selected")
    symbol = str(record.get("trading_symbol", "")).strip()
    if not symbol or len(symbol) > 80:
        raise ValueError("trading_symbol is required and must be <= 80 characters")
    return ForexContract(
        pair=MVP_PAIR,
        jurisdiction=JURISDICTION,
        exchange=EXCHANGE,
        segment=EXCHANGE_SEGMENT,
        product=PRODUCT,
        expiry=expiry,
        provider="upstox",
        provider_instrument_key=key,
        trading_symbol=symbol,
        units_per_lot=CONTRACT_UNITS,
        tick_size=TICK_SIZE,
        pip_size=PIP_SIZE,
        provider_lot_size=lot_size,
        provider_tick_size=tick,
    )


@dataclass(frozen=True)
class ObservedQuote:
    """Quote evidence; timestamps and market status are mandatory."""

    provider: str
    instrument_key: str
    bid: Decimal
    ask: Decimal
    exchange_time: datetime
    received_time: datetime
    market_status: str
    market_status_time: datetime
    provenance: str = "OBSERVED"

    @classmethod
    def create(cls, *, provider: str, instrument_key: str, bid: Any, ask: Any,
               exchange_time: datetime, received_time: datetime,
               market_status: str, market_status_time: datetime,
               provenance: str = "OBSERVED") -> "ObservedQuote":
        for name, value in (("exchange_time", exchange_time),
                            ("received_time", received_time),
                            ("market_status_time", market_status_time)):
            if (not isinstance(value, datetime) or value.tzinfo is None
                    or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        return cls(str(provider).lower(), str(instrument_key), _decimal(bid, "bid"),
                   _decimal(ask, "ask"), exchange_time, received_time,
                   str(market_status), market_status_time, str(provenance))

    def validate(self, contract: ForexContract, *, now: datetime,
                 max_age_seconds: float = QUOTE_MAX_AGE_SECONDS) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if self.provider != "upstox" or contract.provider != "upstox":
            raise ValueError("quote provider must match the documented Upstox data path")
        if self.instrument_key != contract.provider_instrument_key:
            raise ValueError("quote instrument does not match the selected contract")
        if self.provenance != "OBSERVED":
            raise ValueError("only explicitly observed quote evidence may drive paper fills")
        if self.bid > self.ask:
            raise ValueError("bid must not exceed ask")
        if not _aligned(self.bid, contract.tick_size) or not _aligned(self.ask, contract.tick_size):
            raise ValueError("bid and ask must align to the exchange tick")
        age = (now.astimezone(timezone.utc) - self.received_time.astimezone(timezone.utc)).total_seconds()
        exchange_age = (now.astimezone(timezone.utc) - self.exchange_time.astimezone(timezone.utc)).total_seconds()
        try:
            max_age_valid = (
                not isinstance(max_age_seconds, bool)
                and isinstance(max_age_seconds, (int, float))
                and math.isfinite(float(max_age_seconds))
                and max_age_seconds > 0
            )
        except (OverflowError, TypeError, ValueError):
            max_age_valid = False
        if (not max_age_valid or age < 0 or exchange_age < 0
                or age > max_age_seconds or exchange_age > max_age_seconds):
            raise ValueError("quote is stale, future-dated, or has invalid freshness")
        exchange_receive_delta = (self.received_time.astimezone(timezone.utc)
                                  - self.exchange_time.astimezone(timezone.utc)).total_seconds()
        status_age = (now.astimezone(timezone.utc)
                      - self.market_status_time.astimezone(timezone.utc)).total_seconds()
        if exchange_receive_delta < -1 or exchange_receive_delta > max_age_seconds:
            raise ValueError("exchange quote timestamp is inconsistent with receive time")
        if status_age < 0 or status_age > max_age_seconds:
            raise ValueError("market-status observation is stale or future-dated")
        if self.market_status != "NORMAL_OPEN":
            raise ValueError("NSE currency-derivatives segment is not freshly observed NORMAL_OPEN")


def weekday_session_hint(at: datetime) -> dict:
    """Informational clock only; holiday status is deliberately UNKNOWN.

    NSE currency-futures sessions use fixed IST, so DST conversion is not
    applicable. This helper is never sufficient to authorize an execution.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("session timestamp must be timezone-aware")
    local = at.astimezone(ZoneInfo(MARKET_TZ))
    is_weekday = local.weekday() < 5
    within_hours = "09:00" <= local.strftime("%H:%M") < "17:00"
    return {
        "market": EXCHANGE,
        "segment": EXCHANGE_SEGMENT,
        "timezone": MARKET_TZ,
        "local_time": local.isoformat(timespec="seconds"),
        "weekday_hours_hint": "EXPECTED_OPEN" if is_weekday and within_hours else "EXPECTED_CLOSED",
        "holiday_status": "UNKNOWN",
        "dst": "not_applicable_fixed_IST",
        "provider_market_status": "NOT_OBSERVED",
        "execution_authorized": False,
        "note": "Calendar hints never substitute for fresh NSE currency-segment status.",
    }


@dataclass
class PaperPosition:
    side: str
    lots: int
    entry_price: Decimal
    reserved_notional: Decimal
    opened_at: str
    contract_identity: str


@dataclass
class ForexPaperBook:
    """Small isolated full-notional-reserved paper book; no broker order route.

    Positions are discrete futures lots, and the simulator reserves the full
    contract notional as a conservative *notional* cap (not an exchange margin
    calculation or a maximum-loss guarantee). Fills are immediate, all-or-none
    at observed top-of-book prices; depth, slippage, fees, expiry/settlement,
    partial fills, and alerts are not modelled. One net position per contract is
    supported. The caller must persist ``dump()``; this in-memory class does not
    claim durable accounting or broker reconciliation.
    """

    initial_cash_inr: Decimal
    max_lots: int = 1
    max_notional_inr: Decimal = Decimal("1000000")
    max_daily_loss_inr: Decimal = Decimal("5000")
    emergency_stop: bool = False
    cash_inr: Decimal = field(init=False)
    position: PaperPosition | None = field(init=False, default=None)
    realized_pnl_inr: Decimal = field(init=False, default=Decimal(0))
    _idempotency: dict[str, dict] = field(init=False, default_factory=dict)
    _day: date | None = field(init=False, default=None)
    _daily_start_equity: Decimal = field(init=False)

    def __post_init__(self):
        self.initial_cash_inr = _decimal(self.initial_cash_inr, "initial_cash_inr")
        self.max_notional_inr = _decimal(self.max_notional_inr, "max_notional_inr")
        self.max_daily_loss_inr = _decimal(self.max_daily_loss_inr, "max_daily_loss_inr")
        if type(self.max_lots) is not int or self.max_lots <= 0:
            raise ValueError("max_lots must be a positive integer")
        self.cash_inr = self.initial_cash_inr
        self._daily_start_equity = self.initial_cash_inr

    def _fresh_quote(self, contract: ForexContract, quote: ObservedQuote, now: datetime):
        quote.validate(contract, now=now)

    def _check_entry_risk(self, contract: ForexContract, quote: ObservedQuote,
                          now: datetime) -> None:
        if self.emergency_stop:
            raise ValueError("paper emergency stop is active")
        local_day = now.astimezone(ZoneInfo(MARKET_TZ)).date()
        if self._day != local_day:
            self._day = local_day
            self._daily_start_equity = self.equity(contract, quote)
        if self.equity(contract, quote) - self._daily_start_equity <= -self.max_daily_loss_inr:
            raise ValueError("paper daily-loss limit reached")

    def open(self, *, contract: ForexContract, quote: ObservedQuote, side: str,
             lots: int, idempotency_key: str, now: datetime) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("a non-empty paper idempotency_key is required")
        if idempotency_key in self._idempotency:
            prior = self._idempotency[idempotency_key]
            if (prior.get("operation") != "OPEN" or prior.get("contract") != contract.identity
                    or prior.get("request") != {"side": side, "lots": lots}):
                raise ValueError("idempotency key was reused for a different paper request")
            return dict(prior["result"], idempotent_replay=True)
        self._fresh_quote(contract, quote, now)
        self._check_entry_risk(contract, quote, now)
        _validate_lots(lots)
        if lots > self.max_lots:
            raise ValueError("lots exceed the paper max_lots limit")
        if side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if self.position is not None:
            raise ValueError("one net position per contract; close it before reversing")
        fill = contract.price(quote.ask if side == "LONG" else quote.bid)
        notional = contract.notional_inr(fill, lots)
        if notional > self.max_notional_inr:
            raise ValueError("notional exceeds the paper max_notional_inr limit")
        if notional > self.cash_inr:
            raise ValueError("insufficient paper cash for full-notional collateral")
        self.cash_inr -= notional
        self.position = PaperPosition(side, lots, fill, notional,
                                      now.astimezone(timezone.utc).isoformat(), contract.identity)
        result = {"mode": "PAPER", "status": "SIMULATED_FILLED", "side": side,
                  "lots": lots, "fill_price": str(fill), "reserved_notional_inr": str(notional),
                  "contract_identity": contract.identity,
                  "provider_key": contract.provider_instrument_key,
                  "order_id": uuid4().hex, "broker_mutation": False}
        self._idempotency[idempotency_key] = {
            "operation": "OPEN", "contract": contract.identity,
            "request": {"side": side, "lots": lots}, "result": result,
        }
        return dict(result)

    def close(self, *, contract: ForexContract, quote: ObservedQuote,
              idempotency_key: str, now: datetime) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("a non-empty paper idempotency_key is required")
        if idempotency_key in self._idempotency:
            prior = self._idempotency[idempotency_key]
            if prior.get("operation") != "CLOSE" or prior.get("contract") != contract.identity:
                raise ValueError("idempotency key was reused for a different operation")
            return dict(prior["result"], idempotent_replay=True)
        self._fresh_quote(contract, quote, now)
        position = self.position
        if position is None or position.contract_identity != contract.identity:
            raise ValueError("no open paper position for this contract")
        fill = contract.price(quote.bid if position.side == "LONG" else quote.ask)
        pnl = contract.pnl_inr(position.side, position.entry_price, fill, position.lots)
        self.cash_inr += position.reserved_notional + pnl
        self.realized_pnl_inr += pnl
        result = {"mode": "PAPER", "status": "SIMULATED_FILLED", "side": "CLOSE",
                  "position_side": position.side, "lots": position.lots,
                  "fill_price": str(fill), "realized_pnl_inr": str(pnl),
                  "released_notional_inr": str(position.reserved_notional),
                  "contract_identity": contract.identity,
                  "provider_key": contract.provider_instrument_key,
                  "order_id": uuid4().hex, "broker_mutation": False}
        self.position = None
        self._idempotency[idempotency_key] = {"operation": "CLOSE", "contract": contract.identity,
                                               "result": result}
        return dict(result)

    def equity(self, contract: ForexContract, quote: ObservedQuote) -> Decimal:
        if self.position is None:
            return self.cash_inr
        if self.position.contract_identity != contract.identity:
            raise ValueError("mark contract does not match open paper position")
        mark = contract.price(quote.bid if self.position.side == "LONG" else quote.ask)
        unrealized = contract.pnl_inr(self.position.side, self.position.entry_price,
                                      mark, self.position.lots)
        return self.cash_inr + self.position.reserved_notional + unrealized

    def dump(self) -> dict:
        return {
            "mode": "PAPER",
            "initial_cash_inr": str(self.initial_cash_inr),
            "cash_inr": str(self.cash_inr),
            "max_lots": self.max_lots,
            "max_notional_inr": str(self.max_notional_inr),
            "max_daily_loss_inr": str(self.max_daily_loss_inr),
            "emergency_stop": self.emergency_stop,
            "realized_pnl_inr": str(self.realized_pnl_inr),
            "position": ({**asdict(self.position),
                          "entry_price": str(self.position.entry_price),
                          "reserved_notional": str(self.position.reserved_notional)}
                         if self.position else None),
            "idempotency_keys": sorted(self._idempotency),
            "persistence": "CALLER_REQUIRED; this class is in-memory",
        }
