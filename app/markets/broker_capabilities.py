"""Documented broker capabilities for the two LIVE markets AXIOM supports.

This module is **reference data plus a refusal helper**. It does not submit,
cancel or size anything; the execution boundary stays in
:mod:`app.markets.live_execution`. Its job is to make the operator-facing
readiness report and the code say the same, documented thing about each broker,
and to refuse anything a broker does not document instead of translating it.

Documentation read on 2026-11-07 (see ``sources`` on each profile):

* Upstox — order placement, order details/history by ``order_id`` or ``tag``,
  modify, cancel, the order-status appendix, and the ``api-hft.upstox.com``
  enhanced order host.
* Alpaca — Trading API v2 order object and statuses, the create-an-order
  parameters (``client_order_id`` length and documented duplicate rejection),
  and the get-order-by-client-order-id lookup.

The two brokers are **not** modelled as equivalent, and the differences that
matter are recorded explicitly:

* Upstox identifies instruments by ``instrument_key``; Alpaca by ticker symbol.
* Upstox documents ``tag`` as a client-chosen identifier that can be queried
  back, but **not** as a duplicate-rejecting key. Alpaca documents that a second
  submission with the same ``client_order_id`` is rejected. AXIOM therefore
  reconciles by key before any retry on both brokers, and relies on the broker
  to reject duplicates only on Alpaca.
* Products, order types and time-in-force sets differ; whatever is not listed
  as supported is listed under ``unsupported`` with the reason.

Session *evaluation* is deliberately not re-implemented here: NSE sessions come
from :func:`app.markets.nse_reconciliation.nse_session_status` and US sessions
from :class:`app.markets.us_calendar.USEquitiesCalendar`. This module only
records the documented window so the readiness report can print it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DOCUMENTED_AS_OF = "2026-11-07"

BROKER_BY_MARKET: dict[str, str] = {"NSE": "UPSTOX", "NASDAQ": "ALPACA"}


@dataclass(frozen=True)
class SessionFacts:
    """Documented regular session. Evaluation belongs to the existing helpers."""

    label: str
    timezone: str
    open_local: str
    close_local: str
    evaluation: str
    holidays_known: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "timezone": self.timezone,
            "open_local": self.open_local,
            "close_local": self.close_local,
            "evaluation": self.evaluation,
            "holidays_known": self.holidays_known,
        }


@dataclass(frozen=True)
class IdempotencyFacts:
    field: str
    max_length: int
    lookup: str
    rejects_duplicates: bool
    documented: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "max_length": self.max_length,
            "lookup": self.lookup,
            "rejects_duplicates": self.rejects_duplicates,
            "documented": self.documented,
        }


@dataclass(frozen=True)
class MarketCapabilities:
    market: str
    broker: str
    label: str
    instrument_class: str
    instrument_identifier: str
    order_types: tuple[str, ...]
    time_in_force: tuple[str, ...]
    products: tuple[str, ...]
    session: SessionFacts
    idempotency: IdempotencyFacts
    account_operations: tuple[str, ...]
    order_operations: tuple[str, ...]
    unsupported: tuple[str, ...]
    notes: tuple[str, ...]
    sources: tuple[str, ...]
    documented_as_of: str = DOCUMENTED_AS_OF

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "broker": self.broker,
            "label": self.label,
            "instrument_class": self.instrument_class,
            "instrument_identifier": self.instrument_identifier,
            "order_types": list(self.order_types),
            "time_in_force": list(self.time_in_force),
            "products": list(self.products),
            "session": self.session.as_dict(),
            "idempotency": self.idempotency.as_dict(),
            "account_operations": list(self.account_operations),
            "order_operations": list(self.order_operations),
            "unsupported": list(self.unsupported),
            "notes": list(self.notes),
            "sources": list(self.sources),
            "documented_as_of": self.documented_as_of,
        }

    # -- refusal helper ---------------------------------------------------------

    def check_request(
        self,
        *,
        order_type: str | None = None,
        time_in_force: str | None = None,
        product: str | None = None,
    ) -> None:
        """Raise ``CapabilityRefused`` for anything this broker does not document."""
        for label, value, supported in (
            ("order type", order_type, self.order_types),
            ("time in force", time_in_force, self.time_in_force),
            ("product", product, self.products),
        ):
            if value is None:
                continue
            normalized = str(getattr(value, "value", value)).upper()
            if normalized not in supported:
                raise CapabilityRefused(
                    "UNSUPPORTED_ORDER_TYPE",
                    f"{self.broker} does not document {label} {normalized!r} in AXIOM "
                    f"(supported: {', '.join(supported)})",
                )


class CapabilityRefused(RuntimeError):
    """A documented-capability refusal. Carries a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


_NSE = MarketCapabilities(
    market="NSE",
    broker="UPSTOX",
    label="Upstox (NSE cash equity)",
    instrument_class="cash equity",
    instrument_identifier="instrument_key (e.g. NSE_EQ|INE002A01018)",
    order_types=("MARKET", "LIMIT", "SL", "SL-M"),
    time_in_force=("DAY", "IOC"),
    products=("I", "D"),
    session=SessionFacts(
        label="NSE regular session",
        timezone="Asia/Kolkata",
        open_local="09:15",
        close_local="15:30",
        evaluation="app.markets.nse_reconciliation.nse_session_status",
    ),
    idempotency=IdempotencyFacts(
        field="tag",
        max_length=20,
        lookup="order details by tag",
        rejects_duplicates=False,
        documented="tag is a client-chosen order identifier that can be queried back; "
        "Upstox does not document duplicate rejection, so a retry is only safe after a "
        "tag lookup shows no order.",
    ),
    account_operations=(
        "user profile",
        "funds and margin",
        "short-term positions",
    ),
    order_operations=(
        "place order (enhanced order host)",
        "order details by order_id or tag",
        "order book",
        "modify order",
        "cancel order",
    ),
    unsupported=(
        "derivatives (F&O) — documented by Upstox, not enabled in AXIOM",
        "MTF product — documented, not enabled in AXIOM",
        "cover orders / OCO — documented, not enabled in AXIOM",
        "GTD validity — documented, not enabled in AXIOM",
        "after-market orders (is_amo) — documented, not enabled: AXIOM requires an open session",
        "multi/slice order endpoints — documented, not enabled in AXIOM",
    ),
    notes=(
        "Order, modify and cancel calls target the documented enhanced order host; "
        "authentication and all other calls use the standard API host.",
        "Order rows expose traded_quantity, so a working order with a partial fill is "
        "detectable even while its status still reads open or trigger pending.",
        "NSE orders are submitted as protected multi-leg GTT v3 orders by the execution "
        "adapter (app.markets.live_execution).",
    ),
    sources=(
        "https://upstox.com/developer/api-documentation/place-order/",
        "https://upstox.com/developer/api-documentation/order-details/",
        "https://upstox.com/developer/api-documentation/appendix/order-status/",
        "https://upstox.com/developer/api-documentation/announcements/enhanced-url/",
    ),
)

_NASDAQ = MarketCapabilities(
    market="NASDAQ",
    broker="ALPACA",
    label="Alpaca (US listed equity)",
    instrument_class="US listed equity",
    instrument_identifier="ticker symbol",
    order_types=("MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"),
    time_in_force=("DAY", "GTC", "IOC", "FOK"),
    products=("CASH", "MARGIN"),
    session=SessionFacts(
        label="US regular session",
        timezone="America/New_York",
        open_local="09:30",
        close_local="16:00",
        evaluation="app.markets.us_calendar.USEquitiesCalendar",
    ),
    idempotency=IdempotencyFacts(
        field="client_order_id",
        max_length=128,
        lookup="get order by client order id",
        rejects_duplicates=True,
        documented="client_order_id is a client-supplied unique identifier; Alpaca documents "
        "that a duplicate submission with the same client_order_id is rejected and that the "
        "order can be retrieved by it.",
    ),
    account_operations=(
        "account",
        "clock",
        "positions",
        "account activities",
    ),
    order_operations=(
        "create order",
        "list orders",
        "get order by client order id",
        "cancel order",
        "replace order",
    ),
    unsupported=(
        "options — documented by Alpaca, not enabled in AXIOM",
        "crypto — documented, not enabled in AXIOM",
        "notional orders — documented, not enabled: AXIOM sizes by quantity",
        "bracket / OCO / OTO order classes — documented, not enabled in AXIOM",
        "extended_hours — documented for limit+day, not enabled in AXIOM",
        "fractional quantities — documented for market+day, not enabled in AXIOM",
    ),
    notes=(
        "The paper environment is a separate documented host from the live environment: it "
        "fills at the broker and involves no real money. Both hosts are pinned in "
        "app/alpaca_broker/client.py, the only module allowed to name them.",
        "Alpaca's trading clock endpoint is authoritative for session state; the readiness "
        "probe consults it when broker credentials are configured.",
    ),
    sources=(
        "https://docs.alpaca.markets/reference/postorder",
        "https://docs.alpaca.markets/reference/getorderbyclientid",
        "https://docs.alpaca.markets/docs/orders-at-alpaca",
        "https://docs.alpaca.markets/docs/real-time-stock-pricing-data",
    ),
)

CAPABILITIES: dict[str, MarketCapabilities] = {"NSE": _NSE, "NASDAQ": _NASDAQ}


def capabilities_for(market: str) -> MarketCapabilities:
    key = str(getattr(market, "value", market)).upper()
    try:
        return CAPABILITIES[key]
    except KeyError as exc:
        raise CapabilityRefused("UNSUPPORTED_INSTRUMENT", f"unknown market {market!r}") from exc


def describe_capabilities(market: str | None = None) -> Any:
    """Serialisable capability description for the readiness report / API."""
    if market is None:
        return [capabilities_for(name).as_dict() for name in CAPABILITIES]
    return capabilities_for(market).as_dict()


def idempotency_asymmetry() -> dict[str, Any]:
    """The one difference that changes retry logic — stated, not inferred."""
    return {
        "nse": {
            "broker": _NSE.broker,
            "field": _NSE.idempotency.field,
            "broker_rejects_duplicates": _NSE.idempotency.rejects_duplicates,
            "axiom_rule": "look the tag up before any retry; never assume the broker blocked a double submit",
        },
        "nasdaq": {
            "broker": _NASDAQ.broker,
            "field": _NASDAQ.idempotency.field,
            "broker_rejects_duplicates": _NASDAQ.idempotency.rejects_duplicates,
            "axiom_rule": "the broker rejects a duplicate client_order_id; AXIOM still reconciles before retrying",
        },
    }
