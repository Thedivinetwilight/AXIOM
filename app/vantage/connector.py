"""Authenticated, account-specific Vantage MT5 connector.

All broker data and execution come through the same official terminal gateway.
This module performs no order retries and has no live/demo auto-selection logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from math import isfinite
from typing import Any

from app.vantage.models import (
    AccountSnapshot, ConnectionState, Contract, Quote, SessionMode,
    iso_now, mask_login, public_dict, utcnow,
)


AXIOM_MAGIC = 26042026
TRADE_MODE_LABELS = {
    0: "DISABLED", 1: "LONG_ONLY", 2: "SHORT_ONLY", 3: "CLOSE_ONLY", 4: "FULL",
}
ORDER_STATE_LABELS = {
    0: "STARTED", 1: "PLACED", 2: "CANCELLED", 3: "PARTIAL",
    4: "FILLED", 5: "REJECTED", 6: "EXPIRED", 7: "REQUEST_ADD",
    8: "REQUEST_MODIFY", 9: "REQUEST_CANCEL",
}


class BrokerError(RuntimeError):
    """Safe broker error. Messages never include credentials or raw payloads."""


class BrokerUnavailable(BrokerError):
    pass


class AuthenticationFailed(BrokerError):
    pass


class IdentityMismatch(BrokerError):
    pass


class InvalidMarketData(BrokerError):
    pass


def _float(value, default=None):
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _time(value, milliseconds=False) -> str | None:
    number = _float(value)
    if number is None or number <= 0:
        return None
    seconds = number / 1000 if milliseconds else number
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _text(value, limit=300) -> str:
    return str(value or "")[:limit]


def classify_product(symbol: str, path: str, description: str) -> tuple[str, bool]:
    """Conservative Vantage product label from broker-supplied classification.

    Share/ETF products at Vantage are CFDs, not owned underlying securities.
    A generic unrecognized symbol remains UNKNOWN and cannot be traded.
    """
    text = f"{path} {description}".lower()
    symbol_text = symbol.lower()
    if "etf" in text:
        return "ETF_CFD", True
    if any(word in text for word in ("share", "stock", "equities", "equity")):
        return "SHARE_CFD", True
    if any(word in text for word in ("forex", "fx\\", "fx ", "currency")):
        return "FOREX", False
    if "indices" in text or "index" in text:
        return "INDEX_CFD", True
    if any(word in text for word in ("commodit", "metal", "energy", "oil", "gold", "silver")):
        return "COMMODITY_CFD", True
    if any(word in text for word in ("crypto", "digital")):
        return "CRYPTO_CFD", True
    if "bond" in text:
        return "BOND_CFD", True
    if "cfd" in text:
        return "OTHER_CFD", True
    # Common FX symbols are only a display fallback when the broker's path is
    # empty; six adjacent ASCII letters are not sufficient to prove tradability.
    if len(symbol_text) == 6 and symbol_text.isalpha():
        return "FOREX_UNCONFIRMED", False
    return "UNKNOWN", False


def normalize_contract(raw: dict, account_currency: str | None) -> Contract:
    symbol = _text(raw.get("name") or raw.get("symbol"), 64)
    path = _text(raw.get("path"), 300)
    description = _text(raw.get("description") or raw.get("basis"), 300)
    product_type, is_cfd = classify_product(symbol, path, description)
    trade_mode = _int(raw.get("trade_mode"))
    order_mode = _int(raw.get("order_mode"))
    filling_mode = _int(raw.get("filling_mode"))
    execution_mode = _int(raw.get("trade_exemode", raw.get("execution_mode")))
    supported_filling = []
    if filling_mode is not None:
        if filling_mode & 1:
            supported_filling.append("FOK")
        if filling_mode & 2:
            supported_filling.append("IOC")
        if execution_mode != 2:
            supported_filling.append("RETURN")
    contract = Contract(
        symbol=symbol,
        display_name=description or symbol,
        description=description,
        path=path,
        exchange=_text(raw.get("exchange"), 160) or None,
        product_type=product_type,
        is_cfd=is_cfd,
        currency_base=_text(raw.get("currency_base"), 16) or None,
        currency_profit=_text(raw.get("currency_profit"), 16) or None,
        account_currency=account_currency,
        volume_min=_float(raw.get("volume_min")),
        volume_max=_float(raw.get("volume_max")),
        volume_step=_float(raw.get("volume_step")),
        volume_limit=_float(raw.get("volume_limit")),
        contract_size=_float(raw.get("trade_contract_size")),
        tick_size=_float(raw.get("trade_tick_size")),
        tick_value=_float(raw.get("trade_tick_value")),
        digits=_int(raw.get("digits")),
        point=_float(raw.get("point")),
        trade_mode=trade_mode,
        trade_mode_label=TRADE_MODE_LABELS.get(trade_mode, "UNKNOWN"),
        execution_mode=execution_mode,
        filling_mode=filling_mode,
        supported_filling_modes=supported_filling,
        order_mode=order_mode,
        supports_market=bool(order_mode is not None and order_mode & 1),
        supports_limit=bool(order_mode is not None and order_mode & 2),
        supports_stop_loss=bool(order_mode is not None and order_mode & 16),
        supports_take_profit=bool(order_mode is not None and order_mode & 32),
        stops_level_points=_int(raw.get("trade_stops_level")),
        freeze_level_points=_int(raw.get("trade_freeze_level")),
        margin_initial=_float(raw.get("margin_initial")),
        margin_maintenance=_float(raw.get("margin_maintenance")),
        swap_long=_float(raw.get("swap_long")),
        swap_short=_float(raw.get("swap_short")),
        swap_mode=_int(raw.get("swap_mode")),
        swap_rollover3days=_text(raw.get("swap_rollover3days"), 30) or None,
        session_deals=_int(raw.get("session_deals")),
        session_buy_orders=_int(raw.get("session_buy_orders")),
        session_sell_orders=_int(raw.get("session_sell_orders")),
        sessions_available=False,
        next_session_transition=None,
        visible=bool(raw.get("visible", raw.get("select", False))),
    )
    issues = []
    if not symbol:
        issues.append("broker symbol is missing")
    if product_type in {"UNKNOWN", "FOREX_UNCONFIRMED"}:
        issues.append("product type is not confirmed by broker metadata")
    for field, label in (
        ("volume_min", "minimum volume"), ("volume_max", "maximum volume"),
        ("volume_step", "volume step"), ("contract_size", "contract size"),
        ("tick_size", "tick size"), ("tick_value", "tick value"), ("point", "point size"),
    ):
        if getattr(contract, field) is None or getattr(contract, field) <= 0:
            issues.append(f"{label} is missing or invalid")
    if contract.digits is None or not 0 <= contract.digits <= 12:
        issues.append("price precision is missing or invalid")
    if not contract.currency_profit:
        issues.append("profit/quote currency is missing")
    for field, label in (
        ("trade_mode", "trade mode"), ("execution_mode", "execution mode"),
        ("filling_mode", "filling modes"), ("order_mode", "order modes"),
        ("stops_level_points", "stop distance"), ("freeze_level_points", "freeze distance"),
    ):
        if getattr(contract, field) is None:
            issues.append(f"{label} is unavailable")
    if trade_mode == 0:
        issues.append("symbol trading is disabled")
    if order_mode is not None and not (order_mode & 1 or order_mode & 2):
        issues.append("neither market nor limit orders are supported")
    # MT5's Python symbol_info API does not expose a portable next-session
    # calendar. This is explicitly reported, not guessed from symbol names.
    contract.metadata_issues = issues
    contract.metadata_complete = not issues
    contract.tradable = not issues
    return contract


def normalize_order(raw: dict, *, history=False) -> dict:
    ticket = str(raw.get("ticket") or raw.get("order") or "")
    order_type_num = _int(raw.get("type"), -1)
    side = {0: "BUY", 1: "SELL", 2: "BUY", 3: "SELL", 4: "BUY", 5: "SELL",
            6: "BUY", 7: "SELL"}.get(order_type_num, "UNKNOWN")
    order_type = {0: "MARKET", 1: "MARKET", 2: "LIMIT", 3: "LIMIT"}.get(
        order_type_num, "OTHER"
    )
    state_num = _int(raw.get("state"), -1)
    timestamp = (
        _time(raw.get("time_done_msc"), True) or _time(raw.get("time_done"))
        if history else
        _time(raw.get("time_setup_msc"), True) or _time(raw.get("time_setup"))
    )
    magic = _int(raw.get("magic"), 0)
    comment = _text(raw.get("comment"), 160)
    return {
        "order_id": ticket,
        "symbol": _text(raw.get("symbol"), 64),
        "side": side,
        "order_type": order_type,
        "volume_initial": _float(raw.get("volume_initial"), 0.0),
        "volume_current": _float(raw.get("volume_current"), 0.0),
        "price": _float(raw.get("price_open"), 0.0),
        "stop_loss": _float(raw.get("sl"), 0.0) or None,
        "take_profit": _float(raw.get("tp"), 0.0) or None,
        "state": ORDER_STATE_LABELS.get(state_num, _text(raw.get("state"), 40) or "UNKNOWN"),
        "timestamp": timestamp,
        "magic": magic,
        "comment": comment,
        "managed": magic == AXIOM_MAGIC and comment.startswith("AXM"),
    }


def normalize_position(raw: dict) -> dict:
    ticket = str(raw.get("ticket") or raw.get("identifier") or "")
    kind = _int(raw.get("type"), -1)
    magic = _int(raw.get("magic"), 0)
    comment = _text(raw.get("comment"), 160)
    return {
        "position_id": ticket,
        "symbol": _text(raw.get("symbol"), 64),
        "side": "BUY" if kind == 0 else "SELL" if kind == 1 else "UNKNOWN",
        "volume": _float(raw.get("volume"), 0.0),
        "price_open": _float(raw.get("price_open"), 0.0),
        "price_current": _float(raw.get("price_current"), 0.0),
        "stop_loss": _float(raw.get("sl"), 0.0) or None,
        "take_profit": _float(raw.get("tp"), 0.0) or None,
        "profit": _float(raw.get("profit"), 0.0),
        "swap": _float(raw.get("swap"), 0.0),
        "timestamp": _time(raw.get("time_msc"), True) or _time(raw.get("time")),
        "magic": magic,
        "comment": comment,
        "managed": magic == AXIOM_MAGIC and comment.startswith("AXM"),
    }


def normalize_deal(raw: dict) -> dict:
    kind = _int(raw.get("type"), -1)
    entry = _int(raw.get("entry"), -1)
    timestamp = (
        datetime.fromtimestamp(raw["time_msc"] / 1000, timezone.utc).isoformat()
        if _float(raw.get("time_msc"), 0) > 0 else _time(raw.get("time"))
    )
    return {
        "deal_id": str(raw.get("ticket") or raw.get("deal") or ""),
        "order_id": str(raw.get("order") or "") or None,
        "position_id": str(raw.get("position_id") or "") or None,
        "symbol": _text(raw.get("symbol"), 64),
        "side": "BUY" if kind == 0 else "SELL" if kind == 1 else "UNKNOWN",
        "entry": {0: "IN", 1: "OUT", 2: "IN_OUT", 3: "OUT_BY"}.get(entry, "UNKNOWN"),
        "volume": _float(raw.get("volume"), 0.0),
        "price": _float(raw.get("price"), 0.0),
        "profit": _float(raw.get("profit"), 0.0),
        "commission": _float(raw.get("commission"), 0.0),
        "swap": _float(raw.get("swap"), 0.0),
        "fee": _float(raw.get("fee"), 0.0),
        "timestamp": timestamp,
        "magic": _int(raw.get("magic"), 0),
        "comment": _text(raw.get("comment"), 160),
    }


class MT5Connector:
    def __init__(self, gateway):
        self.gateway = gateway
        self.state = ConnectionState.DISCONNECTED
        self.state_detail = "Not connected"
        self.connected = False
        self.account: AccountSnapshot | None = None
        self.contracts: dict[str, Contract] = {}
        self.quotes: dict[str, Quote] = {}
        self._expected_login = ""
        self._expected_server = ""
        self._expected_mode: SessionMode | None = None
        self.last_snapshot: dict | None = None
        self.last_reconciled_at: str | None = None

    @property
    def identity_fingerprint(self) -> str | None:
        if not self._expected_login or not self._expected_server or not self._expected_mode:
            return None
        value = f"{self._expected_mode.value}|{self._expected_server}|{self._expected_login}"
        return sha256(value.encode()).hexdigest()

    def _error_code(self) -> str:
        error = self.gateway.last_error()
        return str(error[0]) if error else "unknown"

    def _safe_disconnect(self):
        try:
            self.gateway.shutdown()
        except Exception:
            pass
        self.connected = False

    def connect(self, credentials: dict, expected_mode: SessionMode) -> AccountSnapshot:
        if self.connected:
            raise BrokerError("Disconnect before changing the MT5 account")
        self.state = ConnectionState.CONNECTING
        self.state_detail = "Initializing the official MT5 terminal connection"
        login = str(credentials.get("login") or "").strip()
        password = str(credentials.get("password") or "")
        server = str(credentials.get("server") or "").strip()
        path = str(credentials.get("terminal_path") or "").strip()
        if not login.isdigit() or not password or not server:
            self.state = ConnectionState.NOT_CONFIGURED
            raise AuthenticationFailed("Login, password and exact MT5 server are required")
        self._expected_login, self._expected_server = login, server
        self._expected_mode = expected_mode
        try:
            initialized = self.gateway.initialize(
                login=int(login), password=password, server=server,
                terminal_path=path, timeout_ms=15_000,
            )
        except Exception as exc:
            self.state = ConnectionState.UNAVAILABLE
            self.state_detail = "MT5 terminal/package could not be initialized"
            raise BrokerUnavailable(self.state_detail) from exc
        if not initialized:
            self.state = ConnectionState.AUTH_FAILED
            self.state_detail = (
                f"MT5 initialization/authentication failed (terminal code {self._error_code()}). "
                "Check the terminal, account and server; no order was sent."
            )
            raise AuthenticationFailed(self.state_detail)
        try:
            account = self.gateway.account_info()
            terminal = self.gateway.terminal_info()
            if not account or not terminal:
                raise AuthenticationFailed("MT5 returned no account/terminal identity")
            if str(account.get("login") or "") != login or str(account.get("server") or "") != server:
                raise IdentityMismatch("Connected MT5 account/server does not exactly match the selected profile")
            account_mode = _int(account.get("trade_mode"), -1)
            if expected_mode == SessionMode.DEMO and account_mode != 0:
                raise IdentityMismatch("VANTAGE DEMO requires an MT5 demo account, not real/contest mode")
            if expected_mode == SessionMode.LIVE and account_mode != 2:
                raise IdentityMismatch("VANTAGE LIVE requires an MT5 real account with exact identity")
            if terminal.get("connected") is not True:
                raise BrokerUnavailable("MT5 terminal reports that it is disconnected")
            self.account = self._account_snapshot(account, terminal)
            self.connected = True
            self.state = ConnectionState.CONNECTED
            self.state_detail = "Connected read-only; no order is authorized by a connection"
            return self.account
        except IdentityMismatch as exc:
            self.state = ConnectionState.IDENTITY_MISMATCH
            self.state_detail = str(exc)
            self._safe_disconnect()
            raise
        except AuthenticationFailed as exc:
            self.state = ConnectionState.AUTH_FAILED
            self.state_detail = str(exc)
            self._safe_disconnect()
            raise
        except Exception as exc:
            self.state = ConnectionState.DISCONNECTED
            self.state_detail = "MT5 connection did not produce a verifiable account"
            self._safe_disconnect()
            if isinstance(exc, BrokerError):
                raise
            raise BrokerUnavailable(self.state_detail) from exc

    def disconnect(self) -> None:
        self._safe_disconnect()
        self.state = ConnectionState.DISCONNECTED
        self.state_detail = "Disconnected by operator; restart never reconnects automatically"
        self.contracts = {}
        self.quotes = {}
        self.last_snapshot = None
        self.last_reconciled_at = None

    def _require_connected(self):
        if not self.connected or not self.account:
            raise BrokerUnavailable("MT5 is not connected to a verified account")

    def _account_snapshot(self, raw: dict, terminal: dict) -> AccountSnapshot:
        trade_mode = _int(raw.get("trade_mode"), -1)
        return AccountSnapshot(
            login_masked=mask_login(raw.get("login")) or "••••",
            server=_text(raw.get("server"), 160),
            currency=_text(raw.get("currency"), 16) or "UNKNOWN",
            balance=_float(raw.get("balance"), 0.0),
            equity=_float(raw.get("equity"), 0.0),
            margin=_float(raw.get("margin"), 0.0),
            margin_free=_float(raw.get("margin_free"), 0.0),
            leverage=_int(raw.get("leverage")),
            trade_allowed=bool(raw.get("trade_allowed", False)),
            expert_allowed=bool(raw.get("trade_expert", False)),
            account_trade_mode={0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(trade_mode, "UNKNOWN"),
            connected=terminal.get("connected") is True,
            terminal_trade_allowed=bool(terminal.get("trade_allowed", False)),
            timestamp=iso_now(),
        )

    def refresh_account(self) -> AccountSnapshot:
        self._require_connected()
        try:
            raw = self.gateway.account_info()
            terminal = self.gateway.terminal_info()
            if not raw or not terminal or terminal.get("connected") is not True:
                raise BrokerUnavailable("MT5 terminal/account is disconnected")
            if str(raw.get("login") or "") != self._expected_login or str(
                raw.get("server") or ""
            ) != self._expected_server:
                raise IdentityMismatch("MT5 account/server changed during the session")
            self.account = self._account_snapshot(raw, terminal)
            return self.account
        except Exception as exc:
            self.connected = False
            self.state = ConnectionState.IDENTITY_MISMATCH if isinstance(exc, IdentityMismatch) else ConnectionState.DISCONNECTED
            self.state_detail = str(exc) if isinstance(exc, BrokerError) else "MT5 account refresh failed"
            if isinstance(exc, BrokerError):
                raise
            raise BrokerUnavailable(self.state_detail) from exc

    def discover(self) -> list[Contract]:
        self._require_connected()
        try:
            rows = self.gateway.symbols_get()
        except Exception as exc:
            raise BrokerUnavailable("MT5 symbol discovery failed") from exc
        if not isinstance(rows, list) or not rows:
            raise BrokerUnavailable("MT5 returned no account-specific instruments")
        contracts = [normalize_contract(row, self.account.currency) for row in rows]
        self.contracts = {contract.symbol: contract for contract in contracts if contract.symbol}
        if not self.contracts:
            raise BrokerUnavailable("MT5 instrument metadata could not be normalized")
        return sorted(self.contracts.values(), key=lambda item: item.symbol)

    def contract(self, symbol: str, refresh=False) -> Contract:
        self._require_connected()
        if refresh or symbol not in self.contracts:
            raw = self.gateway.symbol_info(symbol)
            if not raw:
                raise InvalidMarketData("Symbol is absent from this MT5 account")
            contract = normalize_contract(raw, self.account.currency)
            self.contracts[symbol] = contract
        return self.contracts[symbol]

    def select_symbol(self, symbol: str) -> None:
        contract = self.contract(symbol)
        if not contract.visible:
            try:
                selected = self.gateway.symbol_select(symbol, True)
            except Exception as exc:
                raise BrokerUnavailable("MT5 could not select the symbol in Market Watch") from exc
            if not selected:
                raise InvalidMarketData("MT5 refused to expose the selected symbol")
            self.contracts[symbol] = Contract(**{**contract.__dict__, "visible": True})

    def quote(self, symbol: str) -> Quote:
        self._require_connected()
        try:
            raw = self.gateway.symbol_info_tick(symbol)
        except Exception as exc:
            self.connected = False
            self.state = ConnectionState.DISCONNECTED
            raise BrokerUnavailable("MT5 quote request failed") from exc
        if not raw:
            raise InvalidMarketData("MT5 returned no quote for the selected symbol")
        bid, ask = _float(raw.get("bid")), _float(raw.get("ask"))
        time_msc = _int(raw.get("time_msc"), 0) or _int(raw.get("time"), 0) * 1000
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            raise InvalidMarketData("MT5 quote is missing or has invalid bid/ask prices")
        if time_msc <= 0:
            raise InvalidMarketData("MT5 quote has no valid exchange timestamp")
        timestamp = _time(time_msc, True)
        if not timestamp:
            raise InvalidMarketData("MT5 quote timestamp is invalid")
        quote_time = datetime.fromisoformat(timestamp)
        if (utcnow() - quote_time).total_seconds() < -5:
            raise InvalidMarketData("MT5 quote is from the future; check the terminal clock")
        previous = self.quotes.get(symbol)
        if previous and time_msc < previous.time_msc:
            raise InvalidMarketData("MT5 delivered an out-of-order quote")
        quote = Quote(
            symbol=symbol, bid=bid, ask=ask, last=_float(raw.get("last")),
            timestamp=timestamp, time_msc=time_msc, received_at=iso_now(),
        )
        self.quotes[symbol] = quote
        return quote

    def rates(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        self._require_connected()
        self.select_symbol(symbol)
        mt5_timeframe = {
            "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
            "1h": "H1", "4h": "H4", "1D": "D1",
        }.get(timeframe, timeframe.upper())
        constant = f"TIMEFRAME_{mt5_timeframe}"
        timeframe_value = self.gateway.constant(constant)
        if timeframe_value is None:
            raise ValueError("Unsupported MT5 timeframe")
        rows = self.gateway.copy_rates_from_pos(symbol, timeframe_value, 0, count)
        if not isinstance(rows, list):
            raise BrokerUnavailable("MT5 rate history is unavailable")
        bars, previous = [], None
        for raw in rows:
            timestamp = _time(raw.get("time"))
            values = {name: _float(raw.get(name)) for name in ("open", "high", "low", "close")}
            if not timestamp or any(value is None or value <= 0 for value in values.values()):
                raise InvalidMarketData("MT5 returned an invalid historical bar")
            if values["high"] < max(values["open"], values["close"], values["low"]) or values[
                "low"
            ] > min(values["open"], values["close"], values["high"]):
                raise InvalidMarketData("MT5 returned inconsistent OHLC bars")
            if previous and timestamp <= previous:
                raise InvalidMarketData("MT5 returned duplicate/out-of-order historical bars")
            previous = timestamp
            bars.append({
                "timestamp": timestamp, **values,
                "volume": _float(raw.get("tick_volume", raw.get("volume")), 0.0),
                "spread": _int(raw.get("spread"), 0),
            })
        return bars

    def order_type(self, side: str, order_type: str) -> int:
        name = f"ORDER_TYPE_{side}_{order_type}" if order_type == "LIMIT" else f"ORDER_TYPE_{side}"
        value = self.gateway.constant(name)
        if value is None:
            raise BrokerError(f"MT5 does not expose {name}")
        return int(value)

    def filling(self, contract: Contract, order_type: str) -> int:
        if order_type == "LIMIT":
            value = self.gateway.constant("ORDER_TYPE_FILLING_RETURN")
            if value is None:
                raise BrokerError("MT5 pending-order filling mode is unavailable")
            return int(value)
        if contract.filling_mode is None:
            raise BrokerError("Symbol filling rules are unavailable")
        if contract.filling_mode & 2:
            value = self.gateway.constant("ORDER_TYPE_FILLING_IOC")
        elif contract.filling_mode & 1:
            value = self.gateway.constant("ORDER_TYPE_FILLING_FOK")
        else:
            raise BrokerError("No supported market-order filling mode")
        if value is None:
            raise BrokerError("MT5 filling constants are unavailable")
        return int(value)

    def order_request(
        self, order, contract: Contract, quote: Quote, *, comment: str,
    ) -> dict:
        action_name = "TRADE_ACTION_DEAL" if order.order_type == "MARKET" else "TRADE_ACTION_PENDING"
        action = self.gateway.constant(action_name)
        type_time = self.gateway.constant("ORDER_TIME_GTC")
        if action is None or type_time is None:
            raise BrokerError("MT5 order-action constants are unavailable")
        request = {
            "action": int(action), "symbol": order.symbol, "volume": float(order.volume),
            "type": self.order_type(order.side, order.order_type),
            "deviation": 0, "magic": AXIOM_MAGIC, "comment": comment,
            "type_time": int(type_time), "type_filling": self.filling(contract, order.order_type),
        }
        if order.order_type == "LIMIT":
            request["price"] = float(order.limit_price)
        elif contract.execution_mode != 2:  # market execution must omit price
            request["price"] = float(quote.ask if order.side == "BUY" else quote.bid)
        if order.stop_loss is not None:
            request["sl"] = float(order.stop_loss)
        if order.take_profit is not None:
            request["tp"] = float(order.take_profit)
        return request

    def close_request(self, position: dict, volume: float, contract: Contract, quote: Quote, comment: str) -> dict:
        side = "SELL" if position["side"] == "BUY" else "BUY"
        order = type("CloseInput", (), {
            "symbol": position["symbol"], "side": side, "order_type": "MARKET",
            "volume": volume, "limit_price": None, "stop_loss": None, "take_profit": None,
        })()
        request = self.order_request(order, contract, quote, comment=comment)
        request["position"] = int(position["position_id"])
        return request

    def position_protection_request(
        self, position: dict, stop_loss: float | None, take_profit: float | None,
    ) -> dict:
        """Documented TRADE_ACTION_SLTP request for an existing MT5 position."""
        action = self.gateway.constant("TRADE_ACTION_SLTP")
        if action is None:
            raise BrokerError("MT5 position-protection modification is unavailable")
        ticket = str(position.get("position_id") or "")
        if not ticket.isdigit() or int(ticket) <= 0:
            raise BrokerError("A positive MT5 position ticket is required")
        return {
            "action": int(action), "position": int(ticket),
            "symbol": position["symbol"],
            "sl": float(stop_loss or 0.0), "tp": float(take_profit or 0.0),
        }

    @staticmethod
    def cancel_request(ticket: str) -> dict:
        # TRADE_ACTION_REMOVE is the documented value 8. It is still resolved
        # through the gateway in ``cancel`` below; this helper is for tests/UI.
        return {"action": 8, "order": int(ticket)}

    def check(self, request: dict) -> dict:
        self._require_connected()
        try:
            result = self.gateway.order_check(request)
        except Exception as exc:
            raise BrokerUnavailable("MT5 read-only order check failed") from exc
        if not result:
            raise BrokerUnavailable("MT5 returned no read-only order-check result")
        return self.map_result(result)

    def margin(self, side: str, symbol: str, volume: float, price: float) -> float:
        self._require_connected()
        order_type = self.order_type(side, "MARKET")
        value = self.gateway.order_calc_margin(order_type, symbol, float(volume), float(price))
        value = _float(value)
        if value is None or value < 0:
            raise BrokerUnavailable("MT5 could not estimate account-currency margin")
        return value

    @staticmethod
    def map_result(raw: dict) -> dict:
        return {
            "retcode": _int(raw.get("retcode"), -1),
            "order": str(raw.get("order") or "") or None,
            "deal": str(raw.get("deal") or "") or None,
            "volume": _float(raw.get("volume")),
            "price": _float(raw.get("price")),
            "bid": _float(raw.get("bid")),
            "ask": _float(raw.get("ask")),
            "comment": _text(raw.get("comment"), 300),
            "request_id": _int(raw.get("request_id")),
            "retcode_external": _int(raw.get("retcode_external")),
        }

    def send(self, request: dict) -> dict:
        """One submission attempt only. Ambiguity is for the journal/reconciler."""
        self._require_connected()
        result = self.gateway.order_send(request)  # exceptions deliberately propagate
        if not result:
            raise BrokerUnavailable("MT5 returned no submission result; outcome is unresolved")
        return self.map_result(result)

    def cancel(self, ticket: str) -> dict:
        action = self.gateway.constant("TRADE_ACTION_REMOVE")
        if action is None:
            raise BrokerError("MT5 cancel action is unavailable")
        return self.send({"action": int(action), "order": int(ticket)})

    def snapshot(self, history_days=90) -> dict:
        self._require_connected()
        account = self.refresh_account()
        end = utcnow()
        start = end - timedelta(days=history_days)
        try:
            positions_raw = self.gateway.positions_get()
            orders_raw = self.gateway.orders_get()
            history_raw = self.gateway.history_orders_get(start, end)
            deals_raw = self.gateway.history_deals_get(start, end)
        except Exception as exc:
            self.connected = False
            self.state = ConnectionState.DISCONNECTED
            self.state_detail = "MT5 reconciliation failed; connection is fail-closed"
            raise BrokerUnavailable(self.state_detail) from exc
        if any(not isinstance(rows, list) for rows in (positions_raw, orders_raw, history_raw, deals_raw)):
            raise BrokerUnavailable("MT5 reconciliation returned incomplete account state")
        snapshot = {
            "account": public_dict(self.account),
            "positions": [normalize_position(row) for row in positions_raw],
            "orders": [normalize_order(row) for row in orders_raw],
            "history_orders": [normalize_order(row, history=True) for row in history_raw],
            "deals": [normalize_deal(row) for row in deals_raw],
            "reconciled_at": iso_now(),
            "history_start": start.isoformat(),
            "account_currency": account.currency,
        }
        realized = sum(
            (deal["profit"] or 0) + (deal["commission"] or 0) + (deal["swap"] or 0)
            + (deal["fee"] or 0)
            for deal in snapshot["deals"] if deal["entry"] in {"OUT", "IN_OUT", "OUT_BY"}
        )
        unrealized = sum((position["profit"] or 0) + (position["swap"] or 0) for position in snapshot["positions"])
        snapshot["pnl"] = {
            "realized_visible_history": realized,
            "unrealized": unrealized,
            "history_window_days": history_days,
            "note": "Realized P&L covers only the displayed broker history window.",
        }
        self.last_snapshot = snapshot
        self.last_reconciled_at = snapshot["reconciled_at"]
        return snapshot
