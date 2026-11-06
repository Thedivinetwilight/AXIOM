"""Order intent, lifecycle states and refusal codes shared by both brokers.

The two brokers do not share an order model: Upstox identifies an instrument by
``instrument_key`` and a product/validity pair, Alpaca by a US ticker and a
time-in-force. This module defines the *common subset* AXIOM is willing to
submit, and refuses everything else explicitly instead of translating between
them.

Refusal codes are stable and appear in the durable event log, the readiness
report and the API — never in a broker request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


BROKER_UPSTOX = "UPSTOX"
BROKER_ALPACA = "ALPACA"
BROKERS = (BROKER_UPSTOX, BROKER_ALPACA)


# --------------------------------------------------------------------------- #
# Enumerations (broker-neutral names; adapters map to documented wire values)
# --------------------------------------------------------------------------- #
class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"          # Upstox SL (stop-loss limit) / Alpaca stop
    STOP_MARKET = "STOP_MARKET"  # Upstox SL-M / not available on Alpaca


class TimeInForce(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    GTC = "GTC"


class Product(str, Enum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"


class OrderState(str, Enum):
    """Durable lifecycle of one intent.

    The first three states exist before the broker has acknowledged anything;
    ``AMBIGUOUS`` and ``NEEDS_OPERATOR_REVIEW`` are terminal *until* an operator
    or a successful reconciliation resolves them.
    """

    RECORDED = "RECORDED"                    # intent persisted, not yet sent
    SUBMITTING = "SUBMITTING"                # request in flight (or interrupted)
    ACKNOWLEDGED = "ACKNOWLEDGED"            # broker accepted, nothing filled
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    AMBIGUOUS = "AMBIGUOUS"                  # submit outcome unknown
    NEEDS_OPERATOR_REVIEW = "NEEDS_OPERATOR_REVIEW"


OPEN_STATES = frozenset(
    {
        OrderState.RECORDED,
        OrderState.SUBMITTING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_REQUESTED,
    }
)

TERMINAL_STATES = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)

BLOCKING_STATES = frozenset({OrderState.AMBIGUOUS, OrderState.NEEDS_OPERATOR_REVIEW})

#: Allowed transitions. Anything else raises ``ExecutionRefused("ILLEGAL_TRANSITION")``.
TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.RECORDED: frozenset(
        {
            OrderState.SUBMITTING,
            OrderState.REJECTED,
            OrderState.NEEDS_OPERATOR_REVIEW,
        }
    ),
    OrderState.SUBMITTING: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.REJECTED,
            OrderState.AMBIGUOUS,
            OrderState.FILLED,
            OrderState.PARTIALLY_FILLED,
            OrderState.NEEDS_OPERATOR_REVIEW,
        }
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.AMBIGUOUS,
            OrderState.NEEDS_OPERATOR_REVIEW,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.AMBIGUOUS,
            OrderState.NEEDS_OPERATOR_REVIEW,
        }
    ),
    OrderState.CANCEL_REQUESTED: frozenset(
        {
            OrderState.CANCELLED,
            OrderState.FILLED,
            OrderState.PARTIALLY_FILLED,
            OrderState.ACKNOWLEDGED,
            OrderState.AMBIGUOUS,
            OrderState.NEEDS_OPERATOR_REVIEW,
        }
    ),
    OrderState.AMBIGUOUS: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.NEEDS_OPERATOR_REVIEW,
        }
    ),
    OrderState.NEEDS_OPERATOR_REVIEW: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
        }
    ),
    # terminal states have no outgoing transitions
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
#: Stable refusal codes. Every code names the single condition that failed.
REFUSAL_CODES: dict[str, str] = {
    # gate / activation
    "LIVE_DISABLED": "live execution is not activated for this broker and account",
    "LIVE_KILL_SWITCH": "the kill switch is engaged; no new order may be submitted",
    "BROKER_NOT_CONFIGURED": "the broker is not configured (credentials or enable flag)",
    "ACCOUNT_UNRESOLVED": "the account identity or account state is not resolved",
    "ACCOUNT_NOT_TRADEABLE": "the broker reports the account as not able to trade",
    # limits / risk
    "LIMITS_MISSING": "one or more mandatory risk limits are unset",
    "INSTRUMENT_NOT_ALLOWED": "the instrument is not on the configured allow-list",
    "ORDER_TOO_LARGE": "the order value exceeds max_order_value",
    "EXPOSURE_LIMIT": "the order would exceed a position or total exposure limit",
    "DAILY_ORDER_LIMIT": "the daily order count limit is reached",
    "OPEN_ORDER_LIMIT": "the open-order limit is reached",
    "SESSION_CLOSED": "the instrument's trading session is closed",
    "QUOTE_STALE": "the quote is stale or has no timestamp",
    "QUOTE_MALFORMED": "the quote is missing, non-finite or crossed",
    "PRICE_NOT_TICK": "the price is not a valid tick for this market",
    "ADJUSTED_RESEARCH_PRICE": "an adjusted research price cannot be sent to a broker",
    "UNSUPPORTED_INSTRUMENT": "the broker does not support this instrument class",
    "UNSUPPORTED_ORDER_TYPE": "the broker does not support this order type or time in force",
    "INVALID_INTENT": "the order intent is malformed",
    "ILLEGAL_TRANSITION": "the requested state transition is not legal",
    # lifecycle
    "DUPLICATE_INTENT": "an intent with this id already exists",
    "INTENT_UNKNOWN": "no such intent",
    "SUBMIT_TIMEOUT": "the submission timed out; the outcome is unknown",
    "RECONCILE_FAILED": "the broker could not be asked to confirm the outcome",
    "RECONCILE_MISMATCH": "the broker returned an order that does not match the intent",
    "RETRY_REFUSED": "a retry is refused until the previous attempt is reconciled",
    "RATE_LIMITED": "the broker rate-limited the request",
    "BROKER_ERROR": "the broker returned an error",
    "TRANSPORT_ERROR": "the transport failed before a response was read",
    "RESEARCH_AUTHORITY": "a research verdict cannot authorize execution",
    "KILL_SWITCH_NO_ACK": "engaging or resetting the kill switch requires an explicit operator acknowledgement",
}


class ExecutionRefused(RuntimeError):
    """Fail-closed refusal carrying a stable code and a redacted detail string."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail or REFUSAL_CODES.get(self.code, ""))
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "message": REFUSAL_CODES.get(self.code, "")}


# --------------------------------------------------------------------------- #
# Intent
# --------------------------------------------------------------------------- #
def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


@dataclass(frozen=True)
class ExecutionOrderIntent:
    """One order the operator asked AXIOM to place, with the quote it was priced on."""

    intent_id: str
    broker: str
    account_ref: str
    symbol: str
    instrument_key: str
    side: Side | str
    order_type: OrderType | str
    quantity: float
    time_in_force: TimeInForce | str = TimeInForce.DAY
    product: Product | str = Product.DELIVERY
    limit_price: float | None = None
    trigger_price: float | None = None
    quote_price: float | None = None
    quote_at: str | None = None
    price_basis: str = "live_quote"  # must never be an adjusted research price
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # -- normalisation ---------------------------------------------------------

    def normalized(self) -> "ExecutionOrderIntent":
        return ExecutionOrderIntent(
            intent_id=str(self.intent_id),
            broker=str(getattr(self.broker, "value", self.broker)).upper(),
            account_ref=str(self.account_ref),
            symbol=str(self.symbol).strip().upper(),
            instrument_key=str(self.instrument_key).strip(),
            side=Side(str(getattr(self.side, "value", self.side)).upper()),
            order_type=OrderType(str(getattr(self.order_type, "value", self.order_type)).upper()),
            quantity=float(self.quantity),
            time_in_force=TimeInForce(str(getattr(self.time_in_force, "value", self.time_in_force)).upper()),
            product=Product(str(getattr(self.product, "value", self.product)).upper()),
            limit_price=None if self.limit_price is None else float(self.limit_price),
            trigger_price=None if self.trigger_price is None else float(self.trigger_price),
            quote_price=None if self.quote_price is None else float(self.quote_price),
            quote_at=self.quote_at,
            price_basis=str(self.price_basis),
            created_at=str(self.created_at),
        )

    # -- validation ------------------------------------------------------------

    def validate(self) -> "ExecutionOrderIntent":
        """Return the normalised intent or raise ``ExecutionRefused``.

        Everything checked here is broker-independent. Broker capability checks
        live in :mod:`app.execution.profiles` and are applied by the adapters.
        """
        intent = self.normalized()
        if not intent.intent_id or len(intent.intent_id) > 48:
            raise ExecutionRefused("INVALID_INTENT", "intent_id must be 1-48 characters")
        if intent.broker not in BROKERS:
            raise ExecutionRefused("INVALID_INTENT", f"unknown broker {intent.broker!r}")
        if not intent.account_ref:
            raise ExecutionRefused("ACCOUNT_UNRESOLVED", "account_ref is empty")
        if not intent.symbol:
            raise ExecutionRefused("INVALID_INTENT", "symbol is empty")
        quantity = _finite_positive(intent.quantity)
        if quantity is None or quantity <= 0:
            raise ExecutionRefused("INVALID_INTENT", "quantity must be a positive finite number")
        if intent.price_basis.lower() != "live_quote":
            raise ExecutionRefused(
                "ADJUSTED_RESEARCH_PRICE",
                f"price_basis {intent.price_basis!r} is not a live quote; research prices are never sent",
            )
        if intent.order_type in {OrderType.LIMIT, OrderType.STOP}:
            if _finite_positive(intent.limit_price) is None:
                raise ExecutionRefused("INVALID_INTENT", f"{intent.order_type.value} requires a positive limit_price")
        if intent.order_type in {OrderType.STOP, OrderType.STOP_MARKET}:
            if _finite_positive(intent.trigger_price) is None:
                raise ExecutionRefused(
                    "INVALID_INTENT", f"{intent.order_type.value} requires a positive trigger_price"
                )
        quote = _finite_positive(intent.quote_price)
        if quote is None or quote <= 0:
            raise ExecutionRefused("QUOTE_MALFORMED", "quote_price is missing or not a positive number")
        if not intent.quote_at:
            raise ExecutionRefused("QUOTE_STALE", "quote_at is missing")
        return intent

    def reference_price(self) -> float:
        """Price used for exposure maths: the limit price when present, else the quote."""
        if self.limit_price is not None:
            return float(self.limit_price)
        return float(self.quote_price or 0.0)

    def notional(self) -> float:
        return abs(float(self.quantity) * self.reference_price())

    def as_dict(self) -> dict[str, Any]:
        payload = self.normalized()
        return {
            "intent_id": payload.intent_id,
            "broker": payload.broker,
            "account_ref": payload.account_ref,
            "symbol": payload.symbol,
            "instrument_key": payload.instrument_key,
            "side": payload.side.value,
            "order_type": payload.order_type.value,
            "quantity": payload.quantity,
            "time_in_force": payload.time_in_force.value,
            "product": payload.product.value,
            "limit_price": payload.limit_price,
            "trigger_price": payload.trigger_price,
            "quote_price": payload.quote_price,
            "quote_at": payload.quote_at,
            "price_basis": payload.price_basis,
            "created_at": payload.created_at,
        }


# --------------------------------------------------------------------------- #
# Broker snapshot (what the broker says an order is, right now)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OrderSnapshot:
    """One broker-reported order state, mapped onto AXIOM's lifecycle."""

    broker_order_id: str
    state: OrderState
    filled_quantity: float = 0.0
    average_price: float = 0.0
    broker_status: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "broker_order_id": self.broker_order_id,
            "state": self.state.value,
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
            "broker_status": self.broker_status,
            "reason": self.reason,
        }


def assert_transition(current: OrderState, target: OrderState) -> None:
    """Raise unless ``current -> target`` is a legal lifecycle transition."""
    allowed = TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ExecutionRefused(
            "ILLEGAL_TRANSITION", f"{current.value} -> {target.value} is not a legal transition"
        )
