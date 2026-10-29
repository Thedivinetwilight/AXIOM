"""Typed boundaries for the Upstox daily autonomy session.

Nothing in this module is a broker credential or an execution permission.  In
particular, an Instrument is accepted only after exact Upstox BOD metadata and
account/route checks; a display symbol is never treated as a contract key.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Any, Literal

Side = Literal["BUY", "SELL"]
SessionMode = Literal["PAPER", "UPSTOX_LIVE"]


class Lifecycle(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    READY = "READY"
    ARMED = "ARMED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    EMERGENCY_STOPPED = "EMERGENCY_STOPPED"


class Fault(str, Enum):
    STALE_DATA = "STALE_DATA"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    DISCONNECTED = "DISCONNECTED"
    PROTECTION_REJECTED = "PROTECTION_REJECTED"
    AMBIGUOUS_SUBMISSION = "AMBIGUOUS_SUBMISSION"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    INVALID_DATA = "INVALID_DATA"
    MANUAL_STOP = "MANUAL_STOP"
    RECOVERY = "RECOVERY"
    PERMISSION_REVOKED = "PERMISSION_REVOKED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    MARKET_CLOSED = "MARKET_CLOSED"


@dataclass(frozen=True)
class Instrument:
    instrument_key: str
    display_symbol: str
    segment: str
    exchange: str
    trading_symbol: str
    instrument_type: str
    tick_size: float
    quantity_step: int
    freeze_quantity: int
    contract_multiplier: float | None
    expiry: datetime | None = None
    underlying_key: str = ""
    underlying_symbol: str = ""
    correlation_group: str = ""
    short_route_metadata: bool = False
    product: str = "I"

    @property
    def market_kind(self) -> str:
        if self.segment in {"NSE_EQ", "BSE_EQ"}:
            # The official instruments documentation states instrument_type on
            # equity segments carries the exchange share-series code (EQ, BE,
            # BZ, SM, ST, SG, GS, ...) — every cash series is an equity-segment
            # cash instrument. Only real derivative contract types are routed
            # by the F&O branches below.
            return "EQUITY"
        if self.segment in {"NCD_FO", "BCD_FO"} and self.instrument_type == "FUT":
            return "CURRENCY_FUTURE"
        if self.segment in {"NCD_FO", "BCD_FO"} and self.instrument_type in {"CE", "PE"}:
            return "CURRENCY_OPTION"
        return "UNSUPPORTED"

    @property
    def currency_derivative(self) -> bool:
        return self.market_kind in {"CURRENCY_FUTURE", "CURRENCY_OPTION"}

    def validate(self, *, now: datetime | None = None) -> None:
        if self.market_kind == "UNSUPPORTED":
            raise ValueError(f"Unsupported instrument route: {self.instrument_key}")
        if "|" not in self.instrument_key or self.display_symbol.count("|"):
            raise ValueError("Exact Upstox instrument key and safe display symbol are required")
        values = (self.tick_size, self.quantity_step, self.freeze_quantity)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in values):
            raise ValueError(f"Incomplete contract metadata for {self.instrument_key}")
        if int(self.quantity_step) != self.quantity_step:
            raise ValueError("Quantity step must be an integer")
        if self.contract_multiplier is None or not math.isfinite(self.contract_multiplier) \
                or self.contract_multiplier <= 0:
            raise ValueError(
                f"Exact contract multiplier is required for {self.instrument_key}; "
                "never infer currency contract risk from a display symbol"
            )
        if self.product not in {"I", "D", "MTF"}:
            raise ValueError("Unsupported Upstox product")
        if self.expiry is not None:
            if self.expiry.tzinfo is None:
                raise ValueError("Contract expiry must be timezone-aware")
            if self.market_kind == "EQUITY":
                raise ValueError(
                    f"Cash equities do not expire; raw BOD expiry padding must not be "
                    f"carried as a contract expiry for {self.instrument_key}"
                )
            if now is not None and self.expiry <= now.astimezone(timezone.utc):
                raise ValueError("Expired contracts cannot be selected")
        if self.currency_derivative and not self.underlying_symbol:
            raise ValueError("Currency derivatives require exact underlying metadata")

    def public(self) -> dict[str, Any]:
        result = asdict(self)
        result["expiry"] = self.expiry.isoformat() if self.expiry else None
        result["market_kind"] = self.market_kind
        result["product_label"] = (
            "exchange-listed currency derivative; not spot forex"
            if self.currency_derivative else "Upstox-listed equity"
        )
        result["risk_metadata_complete"] = self.contract_multiplier is not None
        return result


@dataclass(frozen=True)
class Forecast:
    instrument_key: str
    input_end: datetime
    generated_at: datetime
    horizon: int
    mean_close: float
    q10_close: float
    q90_close: float
    up_probability: float
    artifact_digest: str
    model_revision: str
    tokenizer_revision: str
    validated: bool = False
    validation_id: str = ""

    def validate(self, last_close: float) -> None:
        values = (last_close, self.mean_close, self.q10_close, self.q90_close,
                  self.up_probability)
        if not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0:
            raise ValueError("Invalid Kronos forecast values")
        if not 0 <= self.up_probability <= 1:
            raise ValueError("Invalid Kronos directional probability")
        if not self.q10_close <= self.mean_close <= self.q90_close:
            raise ValueError("Invalid Kronos forecast quantiles")
        if self.horizon <= 0 or not self.artifact_digest or not self.model_revision \
                or not self.tokenizer_revision:
            raise ValueError("Forecast provenance is incomplete")

    def public(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("input_end", "generated_at"):
            result[key] = getattr(self, key).isoformat()
        return result


@dataclass(frozen=True)
class StrategyEvidence:
    """Frozen research evidence required before a candidate can be executable.

    Synthetic fixtures may instantiate this in tests, but `synthetic=True`
    permanently prevents the evidence from enabling live orders.
    """

    evidence_id: str
    instrument_key: str
    strategy_family: str
    side: Side
    timeframe: str
    regime: str
    generated_at: datetime
    data_end: datetime
    data_sha256: str
    config_sha256: str
    walk_forward_runs: int
    holdout_runs: int
    trades: int
    net_expectancy: float
    max_drawdown: float
    risk_adjusted_score: float
    baseline_risk_adjusted_score: float
    costs_modelled: bool
    gaps_modelled: bool
    partial_fills_modelled: bool
    rejections_modelled: bool
    survivorship_aware: bool
    upstox_authoritative: bool
    holdout_locked: bool
    synthetic: bool = False
    # This is a trust boundary, not another operator attestation.  Production
    # JSON loading rejects ``provenance_verified=True`` because a file supplied
    # by an operator cannot independently verify its own Upstox provenance.
    # Trusted in-process tests may inject it to exercise LIVE guard logic.
    provenance_verified: bool = False
    kronos_required: bool = False
    kronos_artifact_digest: str = ""
    breakdown: dict[str, Any] = field(default_factory=dict)

    @property
    def provenance_class(self) -> str:
        if self.synthetic:
            return "SYNTHETIC_RESEARCH_ONLY"
        if self.provenance_verified:
            return "UPSTOX_INDEPENDENTLY_VERIFIED"
        return "UPSTOX_JSON_OPERATOR_ATTESTED"

    def executable(self, instrument_key: str, family: str, side: str, timeframe: str,
                   regime: str, *, live: bool, kronos_digest: str = "") -> tuple[bool, str]:
        # Synthetic data remains useful for research/unit tests, but is not an
        # accepted production PAPER or LIVE evidence source.  This deliberately
        # does not relax the prior chronological/cost/provenance gates to force
        # a paper order to occur.
        if self.synthetic:
            return False, "Synthetic evidence is research-only and cannot execute PAPER or LIVE"
        exact = (
            self.instrument_key == instrument_key,
            self.strategy_family == family,
            self.side == side,
            self.timeframe == timeframe,
            self.regime in {regime, "ALL"},
            self.holdout_locked and self.holdout_runs == 1,
            self.walk_forward_runs >= 2,
            self.trades >= 20,
            self.costs_modelled and self.gaps_modelled,
            self.partial_fills_modelled and self.rejections_modelled,
            self.survivorship_aware and self.upstox_authoritative,
            self.net_expectancy > 0,
            self.risk_adjusted_score > self.baseline_risk_adjusted_score,
            self.max_drawdown >= 0,
            not (live and not self.provenance_verified),
            not (live and self.breakdown.get("data_gaps", 0) != 0),
        )
        if not all(exact):
            if live and not self.provenance_verified:
                return False, "LIVE requires independently verified Upstox provenance; operator attestation is insufficient"
            return False, "Missing exact chronological net-of-cost evidence"
        if self.kronos_required and self.kronos_artifact_digest != kronos_digest:
            return False, "Kronos evidence does not match the verified artifact"
        return True, "Validated research evidence matched"


@dataclass(frozen=True)
class Candidate:
    instrument_key: str
    signal_id: str
    bar_timestamp: datetime
    family: str
    side: Side
    entry_price: float
    stop_price: float
    target_price: float
    regime: str
    score: float
    explanation: str
    evidence_id: str = ""
    forecast_audit_id: int | None = None
    kronos_used: bool = False
    fallback_reason: str = ""

    def public(self) -> dict[str, Any]:
        result = asdict(self)
        result["bar_timestamp"] = self.bar_timestamp.isoformat()
        return result


@dataclass(frozen=True)
class DailyRisk:
    trading_day: str
    max_daily_loss: float
    per_trade_risk_fraction: float
    equity_at_entry: float
    entered_at: datetime
    revision: int = 1

    def validate(self) -> None:
        values = (self.max_daily_loss, self.per_trade_risk_fraction, self.equity_at_entry)
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError("Daily loss, per-trade risk and equity must be positive and finite")
        if not 0.0001 <= self.per_trade_risk_fraction <= 0.01:
            raise ValueError("Per-trade risk fraction must be between 0.0001 and 0.01")
        if self.max_daily_loss > self.equity_at_entry * 0.05:
            raise ValueError("Maximum daily loss cannot exceed 5% of entry equity")
        if self.max_daily_loss < self.equity_at_entry * self.per_trade_risk_fraction:
            raise ValueError("Daily loss budget cannot be smaller than one trade's risk")

    def public(self) -> dict[str, Any]:
        result = asdict(self)
        result["entered_at"] = self.entered_at.isoformat()
        return result


@dataclass(frozen=True)
class Position:
    instrument_key: str
    quantity: float
    average_price: float
    last_price: float
    multiplier: float = 1.0
    realised: float = 0.0
    unrealised: float = 0.0
    stop_price: float | None = None
    target_price: float | None = None
    protection_id: str = ""
    correlation_group: str = ""

    @property
    def side(self) -> Side:
        return "BUY" if self.quantity >= 0 else "SELL"

    @property
    def market_value(self) -> float:
        return abs(self.quantity) * self.last_price * self.multiplier

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    available_margin: float
    used_margin: float
    day_pnl: float
    positions: tuple[Position, ...]
    observed_at: datetime
    authoritative: bool

    def validate(self) -> None:
        values = (self.equity, self.available_margin, self.used_margin, self.day_pnl)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Non-finite account state")
        if self.equity <= 0 or self.available_margin < 0 or self.used_margin < 0:
            raise ValueError("Invalid account equity/margin state")


@dataclass(frozen=True)
class AccountProfile:
    active: bool
    exchanges: frozenset[str]
    products: frozenset[str]
    order_types: frozenset[str]
    user_fingerprint: str
    static_ips: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProtectedOrder:
    intent_id: str
    instrument_key: str
    side: Side
    quantity: int
    limit_price: float
    stop_price: float
    target_price: float
    product: str = "I"
    margin_price: float | None = None

    def validate(self) -> None:
        if not self.instrument_key or self.side not in {"BUY", "SELL"} \
                or self.product not in {"I", "D", "MTF"}:
            raise ValueError("Order route is incomplete")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("Quantity must be a positive integer")
        if not math.isfinite(self.limit_price) or self.limit_price < 0:
            raise ValueError("Limit price must be finite and non-negative")
        if not all(math.isfinite(value) and value > 0 for value in
                   (self.stop_price, self.target_price)):
            raise ValueError("Protection prices must be finite and positive")
        if (self.side == "BUY" and not self.stop_price < self.target_price) or \
                (self.side == "SELL" and not self.stop_price > self.target_price):
            raise ValueError("Stop/target side geometry is invalid")
        if self.margin_price is not None and (
                not math.isfinite(self.margin_price) or self.margin_price <= 0):
            raise ValueError("Margin reference price must be finite and positive")


@dataclass(frozen=True)
class ExecutionPermit:
    """Short-lived in-memory capability; never persisted or accepted from JSON."""

    session_id: str
    kind: Literal["ENTRY", "PROTECTIVE", "CANCEL_UNFILLED"]
    nonce: str
    expires_at: datetime

    def validate(self, session_id: str, now: datetime) -> None:
        if self.session_id != session_id or not self.nonce:
            raise PermissionError("Execution permit does not belong to this session")
        if now >= self.expires_at:
            raise PermissionError("Execution permit expired")


@dataclass(frozen=True)
class Submission:
    broker_ids: tuple[str, ...]
    status: str
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


def validate_order_route(order: ProtectedOrder, instrument: Instrument) -> None:
    """Last-line contract validation shared by paper and live adapters."""
    order.validate()
    instrument.validate()
    if order.instrument_key != instrument.instrument_key:
        raise ValueError("Order instrument key does not match the selected contract")
    if order.quantity % int(instrument.quantity_step) or order.quantity > instrument.freeze_quantity:
        raise ValueError("Order quantity violates contract lot/freeze limits")
    prices = [order.stop_price, order.target_price] + ([order.limit_price] if order.limit_price else [])
    for price in prices:
        steps = price / instrument.tick_size
        if not math.isclose(steps, round(steps), rel_tol=0, abs_tol=1e-6):
            raise ValueError("Order price is not aligned to the exact contract tick")
    if order.limit_price:
        if order.side == "BUY" and not order.stop_price < order.limit_price < order.target_price:
            raise ValueError("BUY stop/entry/target geometry is invalid")
        if order.side == "SELL" and not order.target_price < order.limit_price < order.stop_price:
            raise ValueError("SELL stop/entry/target geometry is invalid")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
