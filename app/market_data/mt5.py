"""Authenticated Vantage DEMO market data, isolated from execution.

Only a DPAPI-backed VANTAGE_DEMO credential is read.  The gateway supplied to
this service exposes identification, symbols, ticks and bars—not broker order
or position actions.  All freshness decisions use source timestamps; receive
time is reported separately and never makes an old tick look new.
"""

from __future__ import annotations

from contextlib import nullcontext

from datetime import datetime, timedelta, timezone
import logging
from math import isfinite
from threading import RLock
import time
from typing import Callable

from app.data.models import Candle, TIMEFRAMES, parse_timestamp, validate_symbol
from app.data.validator import validate
from app.market_data.read_only_gateway import ReadOnlyMT5Gateway
from app.vantage.connector import normalize_contract
from app.vantage.models import Quote, SessionMode, mask_login, utcnow
from app.vantage.secrets import SecretNotFound


logger = logging.getLogger("bot.market_data.mt5")
PROVIDER_NAME = "VANTAGE_DEMO_MT5"
_TIMEFRAME_CONSTANT = {
    "1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15",
    "30m": "TIMEFRAME_M30", "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4",
    "1D": "TIMEFRAME_D1",
}
SUPPORTED_TIMEFRAMES = frozenset(_TIMEFRAME_CONSTANT)
TIMEFRAME_CHOICES = tuple(_TIMEFRAME_CONSTANT)


class MarketDataError(RuntimeError):
    """Safe operator error; raw terminal/credential payloads are never echoed."""


class MarketDataAuthenticationError(MarketDataError):
    pass


class MarketDataIdentityError(MarketDataError):
    pass


class MarketDataUnavailable(MarketDataError):
    pass


class MarketDataStale(MarketDataError):
    pass


class MarketDataInvalid(MarketDataError):
    pass


def _finite(value, default=None):
    try:
        number = float(value)
        return number if isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


class MT5MarketDataService:
    def __init__(
        self,
        secret_store,
        execution_mode: Callable[[], SessionMode],
        gateway_factory: Callable = ReadOnlyMT5Gateway,
        *,
        audit: Callable[[str, dict], None] | None = None,
        max_quote_age_seconds: float = 30.0,
        stall_after_seconds: float = 15.0,
        advance_timeout_seconds: float = 2.0,
        poll_seconds: float = 0.1,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        metrics=None,
    ):
        if not 0 < max_quote_age_seconds <= 300:
            raise ValueError("Market-data quote age bound must be in (0, 300]")
        if not 0 < stall_after_seconds <= 300:
            raise ValueError("Market-data stall bound must be in (0, 300]")
        self.secret_store = secret_store
        self.execution_mode = execution_mode
        self.gateway_factory = gateway_factory
        self.audit = audit
        self.metrics = metrics
        self.max_quote_age_seconds = float(max_quote_age_seconds)
        self.stall_after_seconds = float(stall_after_seconds)
        self.advance_timeout_seconds = float(advance_timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = RLock()
        self.gateway = None
        self.state = "DISCONNECTED"
        self.detail = "Not connected; execution remains AXIOM PAPER"
        self.authenticated = False
        self.account: dict | None = None
        self.instruments: dict[str, dict] = {}
        self.selected_symbol: str | None = None
        self.quotes: dict[str, Quote] = {}
        self._last_advance: dict[str, float] = {}
        self._expected_login = ""
        self._expected_server = ""

    def _event(self, kind: str, payload: dict):
        if not self.audit:
            return
        try:
            self.audit(kind, payload)
        except Exception as exc:  # audit failure is logged, never secret-bearing
            logger.error("market_data_audit_failed", extra={"error": type(exc).__name__})

    def _degraded(self, detail: str, *, state: str = "DEGRADED"):
        changed = self.state != state or self.detail != detail
        self.state, self.detail = state, detail
        if changed:
            self._event("MARKET_DATA_DEGRADED", {
                "provider": PROVIDER_NAME, "state": state, "reason": detail,
                "symbol": self.selected_symbol,
            })

    def _shutdown(self):
        gateway, self.gateway = self.gateway, None
        self.authenticated = False
        if gateway is not None:
            try:
                gateway.shutdown()
            except Exception as exc:
                logger.warning("market_data_shutdown_failed", extra={"error": type(exc).__name__})

    def disconnect(self, reason: str = "Disconnected by operator"):
        with self._lock:
            self._shutdown()
            self.state, self.detail = "DISCONNECTED", reason
            self.account = None
            self.instruments = {}
            self.selected_symbol = None
            self.quotes = {}
            self._last_advance = {}
            self._event("MARKET_DATA_DISCONNECTED", {
                "provider": PROVIDER_NAME, "reason": reason,
            })
            return self.status()

    def _require_paper(self):
        if self.execution_mode() != SessionMode.PAPER:
            raise MarketDataUnavailable(
                "Independent MT5 market data is available only while execution is AXIOM PAPER"
            )

    def _require_gateway(self):
        self._require_paper()
        if self.gateway is None or not self.authenticated:
            raise MarketDataUnavailable(
                "Read-only VANTAGE DEMO market data is not authenticated; connect it on Connections"
            )
        try:
            terminal = self.gateway.terminal_info()
        except Exception as exc:
            self._degraded("MT5 terminal information failed; new paper entries are blocked")
            raise MarketDataUnavailable("MT5 terminal status is unavailable") from exc
        if not terminal or terminal.get("connected") is not True:
            self._degraded("MT5 terminal disconnected or shut down; new paper entries are blocked")
            raise MarketDataUnavailable(
                "MT5 terminal is disconnected or shut down; reconnect the demo feed before new entries"
            )
        return self.gateway

    def connect(self):
        with self._lock:
            self._require_paper()
            self._shutdown()
            self.account = None
            self.instruments = {}
            self.selected_symbol = None
            self.quotes = {}
            self._last_advance = {}
            if self.secret_store is None:
                raise MarketDataAuthenticationError(
                    "Windows DPAPI credential storage is unavailable; plaintext fallback is disabled"
                )
            try:
                # This is intentionally the only credential profile read here.
                credential = self.secret_store.get(SessionMode.DEMO.value)
            except SecretNotFound as exc:
                self.state = "NOT_CONFIGURED"
                self.detail = "Save a VANTAGE DEMO credential locally; execution stays AXIOM PAPER"
                raise MarketDataAuthenticationError(self.detail) from exc
            self.state, self.detail = "CONNECTING", "Authenticating read-only VANTAGE DEMO market data"
            gateway = None
            try:
                gateway = self.gateway_factory()
                self.gateway = gateway
                if not gateway.initialize(
                    login=int(credential["login"]), password=credential["password"],
                    server=credential["server"],
                    terminal_path=credential.get("terminal_path", ""), timeout_ms=15_000,
                ):
                    self.state = "AUTHENTICATION_FAILED"
                    self.detail = "MT5 demo authentication failed; verify the locally saved DEMO credential"
                    raise MarketDataAuthenticationError(self.detail)
                account = gateway.account_info()
                terminal = gateway.terminal_info()
                expected_login = str(credential["login"])
                expected_server = str(credential["server"])
                if not account or not terminal or terminal.get("connected") is not True:
                    self.state = "TERMINAL_UNAVAILABLE"
                    self.detail = "MT5 terminal/account identification is unavailable"
                    raise MarketDataUnavailable(self.detail)
                if (
                    str(account.get("login")) != expected_login
                    or str(account.get("server")) != expected_server
                ):
                    self.state = "SERVER_ACCOUNT_MISMATCH"
                    self.detail = "Connected MT5 identity does not match the saved VANTAGE DEMO profile"
                    raise MarketDataIdentityError(self.detail)
                if _integer(account.get("trade_mode"), -1) != 0:
                    self.state = "SERVER_ACCOUNT_MISMATCH"
                    self.detail = "The connected MT5 account is not confirmed DEMO; market data is refused"
                    raise MarketDataIdentityError(self.detail)
                self._expected_login, self._expected_server = expected_login, expected_server
                self.account = {
                    "login_masked": mask_login(expected_login),
                    "server": expected_server,
                    "currency": str(account.get("currency") or "") or None,
                    "account_trade_mode": "DEMO",
                }
                self.authenticated = True
                self.state, self.detail = "CONNECTED", "Authenticated read-only VANTAGE DEMO market data"
                self._discover()
                self._event("MARKET_DATA_CONNECTED", {
                    "provider": PROVIDER_NAME,
                    "account_login_masked": self.account["login_masked"],
                    "server": expected_server,
                    "instruments_discovered": len(self.instruments),
                    "execution_mode": SessionMode.PAPER.value,
                    "automation_enabled": False,
                })
                return self.status()
            except Exception as exc:
                self._shutdown()
                self.authenticated = False
                if self.state in {"CONNECTING", "CONNECTED"}:
                    self.state = "UNAVAILABLE"
                    self.detail = (
                        str(exc) if isinstance(exc, MarketDataError)
                        else "Read-only MT5 market-data connection failed"
                    )
                raise

    def _discover(self):
        gateway = self._require_gateway()
        try:
            rows = gateway.symbols_get()
        except Exception as exc:
            raise MarketDataUnavailable("MT5 symbol discovery failed") from exc
        if rows is None:
            raise MarketDataUnavailable("MT5 symbol discovery returned no result")
        discovered: dict[str, dict] = {}
        currency = (self.account or {}).get("currency")
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            contract = normalize_contract(raw, currency)
            if not contract.symbol or contract.symbol in discovered:
                raise MarketDataInvalid("MT5 returned a missing or duplicate broker symbol")
            item = contract.to_dict()
            try:
                validate_symbol(contract.symbol)
            except ValueError:
                item["metadata_issues"] = list(item["metadata_issues"]) + [
                    "symbol is outside AXIOM's supported API character/length boundary"
                ]
                item["metadata_complete"] = False
                item["tradable"] = False
            item["market_data_supported"] = bool(item["tradable"])
            item["selected"] = False
            item["quote"] = None
            discovered[contract.symbol] = item
        if not discovered:
            raise MarketDataUnavailable("The connected demo account exposed no MT5 instruments")
        self.instruments = discovered
        self.detail = f"Discovered {len(discovered)} account-specific MT5 instruments"

    def instrument(self, symbol: str) -> dict:
        item = self.instruments.get(symbol)
        if item is None:
            raise MarketDataInvalid(
                "Symbol was not returned by this connected demo account; no substitute universe is used"
            )
        result = dict(item)
        result["selected"] = symbol == self.selected_symbol
        quote = self.quotes.get(symbol)
        result["quote"] = self._quote_view(quote) if quote else None
        return result

    def instrument_list(self) -> list[dict]:
        return [self.instrument(symbol) for symbol in sorted(self.instruments, key=str.casefold)]

    def _quote_view(self, quote: Quote):
        now = utcnow()
        age = quote.age_seconds(now)
        since_advance = self._monotonic() - self._last_advance.get(quote.symbol, -1e12)
        fresh = (
            0 <= age <= self.max_quote_age_seconds
            and 0 <= since_advance <= self.stall_after_seconds
            and self.state == "CONNECTED"
        )
        return {
            "symbol": quote.symbol, "bid": quote.bid, "ask": quote.ask,
            "last": quote.last, "timestamp": quote.timestamp,
            "time_msc": quote.time_msc, "received_at": quote.received_at,
            "age_seconds": age, "seconds_since_advance": since_advance,
            "fresh": fresh, "advancing": since_advance <= self.stall_after_seconds,
            "source": PROVIDER_NAME,
        }

    def quote(self, symbol: str) -> Quote:
        with self._lock:
            gateway = self._require_gateway()
            item = self.instrument(symbol)
            if not item["market_data_supported"]:
                raise MarketDataInvalid(
                    "Instrument metadata is incomplete or unsupported; select a supported discovered symbol"
                )
            try:
                timer = self.metrics.measure("market_data.feed.gateway_receipt") if self.metrics else nullcontext()
                with timer:
                    raw = gateway.symbol_info_tick(symbol)
            except Exception as exc:
                self._degraded("MT5 tick read failed; new paper entries are blocked")
                raise MarketDataUnavailable("MT5 tick read failed") from exc
            bid, ask = _finite(raw.get("bid")), _finite(raw.get("ask"))
            last = _finite(raw.get("last"))
            time_msc = _integer(raw.get("time_msc"))
            if time_msc is None:
                seconds = _finite(raw.get("time"))
                time_msc = int(seconds * 1000) if seconds and seconds > 0 else None
            if (
                not raw or bid is None or ask is None or bid <= 0 or ask <= 0
                or ask < bid or time_msc is None or time_msc <= 0
            ):
                self._degraded("MT5 returned a missing/invalid tick; new paper entries are blocked")
                raise MarketDataInvalid("MT5 tick is missing a valid bid/ask or source timestamp")
            try:
                timestamp = datetime.fromtimestamp(time_msc / 1000, timezone.utc)
            except (OverflowError, OSError, ValueError) as exc:
                raise MarketDataInvalid("MT5 tick timestamp is outside the supported range") from exc
            now = utcnow()
            age = (now - timestamp).total_seconds()
            if age < -5:
                self._degraded("MT5 tick timestamp is in the future; new paper entries are blocked")
                raise MarketDataInvalid("MT5 tick timestamp is in the future")
            if self.metrics and age >= 0:
                self.metrics.observe("market_data.feed.observed_source_age", age * 1000.0)
            if age > self.max_quote_age_seconds:
                self._degraded("MT5 quote is stale; new paper entries are blocked")
                raise MarketDataStale(
                    f"MT5 quote is {age:.1f}s old (limit {self.max_quote_age_seconds:g}s); "
                    "check market hours and the demo terminal"
                )
            previous = self.quotes.get(symbol)
            if previous and time_msc < previous.time_msc:
                self._degraded("MT5 tick timestamps moved backwards; new paper entries are blocked")
                raise MarketDataInvalid("MT5 tick timestamps are out of order")
            quote = Quote(
                symbol=symbol, bid=bid, ask=ask,
                last=last if last is not None and last > 0 else None,
                timestamp=timestamp.isoformat(), time_msc=time_msc,
                received_at=now.isoformat(),
            )
            self.quotes[symbol] = quote
            if previous is None or time_msc > previous.time_msc:
                self._last_advance[symbol] = self._monotonic()
            if self.state != "CONNECTED":
                self.state = "CONNECTED"
                self.detail = "Read-only demo quotes are fresh; execution remains AXIOM PAPER"
            return quote

    def ensure_advancing(self, symbol: str):
        """Require two fresh ticks with strictly increasing source timestamps."""
        with self._lock:
            gateway = self._require_gateway()
            item = self.instrument(symbol)  # discovered exact symbol; never a preset ticker
            if not item["market_data_supported"]:
                self._degraded("Instrument metadata is incomplete; new paper entries are blocked")
                raise MarketDataInvalid(
                    "Instrument metadata is incomplete or unsupported; select a supported discovered symbol"
                )
            try:
                subscribed = gateway.symbol_select(symbol, True)
            except Exception as exc:
                self._degraded("MT5 symbol subscription failed; new paper entries are blocked")
                raise MarketDataUnavailable("MT5 symbol subscription failed") from exc
            if not subscribed:
                self._degraded("MT5 refused the symbol subscription; new paper entries are blocked")
                raise MarketDataUnavailable(
                    "MT5 refused the symbol subscription; no substitute symbol is used"
                )
            first = self.quote(symbol)
            deadline = self._monotonic() + self.advance_timeout_seconds
            while self._monotonic() < deadline:
                self._sleep(self.poll_seconds)
                current = self.quote(symbol)
                if current.time_msc > first.time_msc:
                    self.selected_symbol = symbol
                    self.state = "CONNECTED"
                    self.detail = "Fresh advancing VANTAGE DEMO quotes confirmed"
                    self._event("MARKET_DATA_SELECTED", {
                        "provider": PROVIDER_NAME, "symbol": symbol,
                        "quote_timestamp": current.timestamp,
                        "execution_mode": SessionMode.PAPER.value,
                    })
                    return current
            self._degraded("Demo quotes are not advancing; new paper entries are blocked")
            raise MarketDataStale(
                "No advancing MT5 tick arrived before the readiness timeout; "
                "check that this instrument's market is open and try again"
            )

    def entry_block_reason(self, symbol: str) -> str | None:
        try:
            # A recent tick is not evidence that the terminal is still alive.
            # Re-check terminal connectivity at the actual entry guard.
            self._require_gateway()
        except MarketDataError as exc:
            return str(exc)
        if self.gateway is None or not self.authenticated or self.state != "CONNECTED":
            return self.detail or "Read-only demo market data is not connected"
        if self.selected_symbol != symbol:
            return "Select and verify this exact MT5 symbol before a new paper entry"
        quote = self.quotes.get(symbol)
        if quote is None:
            return "No current MT5 quote; reconnect and verify fresh advancing ticks"
        view = self._quote_view(quote)
        if not view["fresh"]:
            return (
                "MT5 quote is stale or not advancing; new paper entries are blocked. "
                "Check the terminal, connection and market hours"
            )
        return None

    def copy_bars(self, symbol: str, timeframe: str, count: int) -> tuple[list[Candle], list[str]]:
        with self._lock:
            gateway = self._require_gateway()
            item = self.instrument(symbol)
            if not item["market_data_supported"]:
                raise MarketDataInvalid("Instrument metadata does not support AXIOM market data")
            if timeframe not in _TIMEFRAME_CONSTANT:
                raise MarketDataInvalid("Unsupported MT5 bar timeframe")
            constant = gateway.constant(_TIMEFRAME_CONSTANT[timeframe])
            if constant is None:
                raise MarketDataUnavailable("MT5 did not expose the requested timeframe")
            try:
                # start=1 excludes the currently forming bar by API contract.
                rows = gateway.copy_rates_from_pos(symbol, constant, 1, int(count))
            except Exception as exc:
                self._degraded("MT5 closed-bar read failed; new paper entries are blocked")
                raise MarketDataUnavailable("MT5 closed-bar read failed") from exc
            if rows is None:
                self._degraded("MT5 closed bars are unavailable; no replay fallback is used")
                raise MarketDataUnavailable("MT5 returned no closed-bar result")
            candles = []
            now = utcnow()
            for raw in rows:
                try:
                    timestamp = datetime.fromtimestamp(float(raw["time"]), timezone.utc)
                    candle = Candle(
                        timestamp=timestamp,
                        open=float(raw["open"]), high=float(raw["high"]),
                        low=float(raw["low"]), close=float(raw["close"]),
                        volume=float(raw.get("tick_volume", raw.get("real_volume", 0)) or 0),
                        symbol=symbol,
                    )
                except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
                    self._degraded("MT5 returned an invalid closed bar; new paper entries are blocked")
                    raise MarketDataInvalid("MT5 closed bar has an invalid timestamp or OHLCV value") from exc
                if candle.available_at(timeframe) > now + timedelta(seconds=2):
                    raise MarketDataInvalid("MT5 returned a forming/future bar where a closed bar was required")
                candles.append(candle)
            if not candles:
                raise MarketDataUnavailable("MT5 returned zero closed bars; no substitute data is used")
            report = validate(candles, timeframe)
            try:
                report.require_valid()
            except ValueError as exc:
                self._degraded("MT5 closed bars failed validation; new paper entries are blocked")
                raise MarketDataInvalid(str(exc)) from exc
            return candles, list(report.warnings)

    def status(self):
        quote = self.quotes.get(self.selected_symbol) if self.selected_symbol else None
        return {
            "provider": PROVIDER_NAME,
            "state": self.state,
            "detail": self.detail,
            "connected": bool(self.gateway is not None and self.authenticated),
            "authenticated": self.authenticated,
            "account": dict(self.account) if self.account else None,
            "credential_profile": SessionMode.DEMO.value,
            "credential_storage": "Windows DPAPI · current Windows user",
            "execution_mode": SessionMode.PAPER.value,
            "read_only": True,
            "orders_modified": False,
            "automation_enabled": False,
            "instrument_count": len(self.instruments),
            "supported_instrument_count": sum(
                bool(item.get("market_data_supported")) for item in self.instruments.values()
            ),
            "selected_symbol": self.selected_symbol,
            "quote": self._quote_view(quote) if quote else None,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "stall_after_seconds": self.stall_after_seconds,
            "live_trading": False,
            "profitability_claim": "none",
        }
