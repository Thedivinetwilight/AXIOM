"""Provider-neutral models for one explicitly selected trading session."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any


class SessionMode(str, Enum):
    PAPER = "AXIOM_PAPER"
    DEMO = "VANTAGE_DEMO"
    LIVE = "VANTAGE_LIVE"

    @property
    def is_vantage(self) -> bool:
        return self in {SessionMode.DEMO, SessionMode.LIVE}


class ConnectionState(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    AUTH_FAILED = "AUTHENTICATION_FAILED"
    IDENTITY_MISMATCH = "SERVER_ACCOUNT_MISMATCH"
    UNAVAILABLE = "TERMINAL_UNAVAILABLE"


class Lifecycle(str, Enum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


FINAL_ORDER_STATES = {"REJECTED", "FILLED", "CANCELLED", "EXPIRED", "MODIFIED"}
UNRESOLVED_ORDER_STATES = {
    "INTENT_RECORDED",
    "SUBMISSION_ATTEMPTED",
    "MODIFICATION_ATTEMPTED",
    "UNKNOWN",
    "UNRESOLVED",
    "ACCEPTED_UNRECONCILED",
    "CANCEL_ATTEMPTED",
    "CANCEL_UNKNOWN",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def mask_login(value: str | int | None) -> str | None:
    """Mask an account identifier without leaking its length or a useful prefix."""
    if value is None or str(value).strip() == "":
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    suffix = digits[-2:] if len(digits) >= 2 else "••"
    return f"••••{suffix}"


def public_dict(value: Any) -> dict:
    """Dataclass/dict conversion for API views, rejecting non-finite numbers."""
    raw = asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)

    def clean(item):
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, dict):
            return {str(k): clean(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        if isinstance(item, float) and not isfinite(item):
            return None
        return item

    return clean(raw)


@dataclass(frozen=True)
class AccountSnapshot:
    login_masked: str
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int | None
    trade_allowed: bool
    expert_allowed: bool
    account_trade_mode: str
    connected: bool
    terminal_trade_allowed: bool
    timestamp: str

    @property
    def trading_permitted(self) -> bool:
        return self.connected and self.trade_allowed and self.terminal_trade_allowed


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float | None
    timestamp: str
    time_msc: int
    received_at: str

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        return self.spread / self.mid * 10_000 if self.mid > 0 else float("inf")

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        stamp = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        return (now - stamp).total_seconds()


@dataclass
class Contract:
    symbol: str
    display_name: str
    description: str
    path: str
    exchange: str | None
    product_type: str
    is_cfd: bool
    currency_base: str | None
    currency_profit: str | None
    account_currency: str | None
    volume_min: float | None
    volume_max: float | None
    volume_step: float | None
    volume_limit: float | None
    contract_size: float | None
    tick_size: float | None
    tick_value: float | None
    digits: int | None
    point: float | None
    trade_mode: int | None
    trade_mode_label: str
    execution_mode: int | None
    filling_mode: int | None
    supported_filling_modes: list[str] = field(default_factory=list)
    order_mode: int | None = None
    supports_market: bool = False
    supports_limit: bool = False
    supports_stop_loss: bool = False
    supports_take_profit: bool = False
    stops_level_points: int | None = None
    freeze_level_points: int | None = None
    margin_initial: float | None = None
    margin_maintenance: float | None = None
    swap_long: float | None = None
    swap_short: float | None = None
    swap_mode: int | None = None
    swap_rollover3days: str | None = None
    session_deals: int | None = None
    session_buy_orders: int | None = None
    session_sell_orders: int | None = None
    sessions_available: bool = False
    next_session_transition: str | None = None
    commission_estimate: float | None = None
    commission_note: str = "Not supplied by the MT5 symbol API; broker statement/deals are authoritative."
    visible: bool = False
    metadata_complete: bool = False
    metadata_issues: list[str] = field(default_factory=list)
    tradable: bool = False

    def to_dict(self) -> dict:
        return public_dict(self)


@dataclass(frozen=True)
class RiskLimits:
    """Broker-session limits. Zero means NOT CONFIGURED, never unlimited."""

    max_order_volume: float = 0.0
    max_symbol_volume: float = 0.0
    max_order_margin: float = 0.0
    max_total_margin: float = 0.0
    max_open_positions: int = 0
    max_daily_loss: float = 0.0
    max_drawdown_pct: float = 0.0
    max_trades_per_day: int = 0
    max_margin_usage_pct: float = 0.0
    max_spread_bps: float = 0.0
    max_slippage_points: int = 0
    max_quote_age_seconds: int = 0

    def issues(self) -> list[str]:
        issues: list[str] = []
        positive = (
            "max_order_volume", "max_symbol_volume", "max_order_margin",
            "max_total_margin", "max_open_positions", "max_daily_loss",
            "max_drawdown_pct", "max_trades_per_day", "max_margin_usage_pct",
            "max_spread_bps", "max_quote_age_seconds",
        )
        for name in positive:
            value = getattr(self, name)
            if not isfinite(float(value)) or float(value) <= 0:
                issues.append(f"{name} must be explicitly configured above zero")
        if not 0 <= self.max_slippage_points <= 100_000:
            issues.append("max_slippage_points must be between 0 and 100000")
        if self.max_order_volume > 1_000_000 or self.max_symbol_volume > 1_000_000:
            issues.append("volume limits exceed the bounded maximum")
        if max(self.max_order_margin, self.max_total_margin, self.max_daily_loss) > 1e12:
            issues.append("monetary limits exceed the bounded maximum")
        if self.max_open_positions > 10_000 or self.max_trades_per_day > 100_000:
            issues.append("count limits exceed the bounded maximum")
        if self.max_drawdown_pct > 100 or self.max_margin_usage_pct > 100:
            issues.append("percentage limits must not exceed 100")
        if self.max_spread_bps > 10_000:
            issues.append("max_spread_bps must not exceed 10000")
        if self.max_quote_age_seconds > 300:
            issues.append("max_quote_age_seconds must not exceed 300")
        if self.max_symbol_volume and self.max_order_volume > self.max_symbol_volume:
            issues.append("max_order_volume cannot exceed max_symbol_volume")
        if self.max_total_margin and self.max_order_margin > self.max_total_margin:
            issues.append("max_order_margin cannot exceed max_total_margin")
        return issues

    def to_dict(self) -> dict:
        return public_dict(self)


@dataclass(frozen=True)
class OrderInput:
    symbol: str
    side: str
    order_type: str
    volume: float
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    idempotency_key: str = ""
    position_id: str | None = None

    def validate(self) -> None:
        if not self.symbol or len(self.symbol) > 64 or any(ord(c) < 32 for c in self.symbol):
            raise ValueError("A valid broker symbol is required")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if self.order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("Only MARKET and LIMIT orders are supported")
        if not isfinite(self.volume) or self.volume <= 0:
            raise ValueError("volume must be finite and above zero")
        for name in ("limit_price", "stop_loss", "take_profit"):
            value = getattr(self, name)
            if value is not None and (not isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and above zero")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("A LIMIT order requires limit_price")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("A MARKET order must not include limit_price")
        if not (8 <= len(self.idempotency_key) <= 80) or not all(
            c.isalnum() or c in "-_" for c in self.idempotency_key
        ):
            raise ValueError("idempotency_key must be 8-80 letters, digits, '-' or '_'")
        if self.position_id is not None and (
            not str(self.position_id).isdigit() or int(self.position_id) <= 0
        ):
            raise ValueError("position_id must be a positive MT5 position ticket")


@dataclass
class ReadinessCheck:
    key: str
    passed: bool
    label: str
    detail: str

    def to_dict(self) -> dict:
        return public_dict(self)
