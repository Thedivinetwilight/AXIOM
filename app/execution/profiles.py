"""Documented broker capabilities — Upstox (NSE) and Alpaca (US equities).

Every field here is a statement taken from the broker's official documentation,
with the URL it came from and the date it was read. The two brokers are **not**
modelled as equivalent: each profile lists only what that broker documents, and
everything else is refused with ``UNSUPPORTED_INSTRUMENT`` /
``UNSUPPORTED_ORDER_TYPE`` rather than translated into a "close enough" order.

Documentation read on 2026-11-07 (see ``SOURCES``):

* Upstox — order placement, order details/history by ``order_id`` or ``tag``,
  modify, cancel, the order-status appendix, and the ``api-hft.upstox.com``
  enhanced order URL announcement.
* Alpaca — Trading API v2 order object and statuses, the Create-an-Order
  parameters (``client_order_id`` length and documented duplicate rejection),
  and the get-order-by-client-order-id lookup. Exact endpoint paths stay in
  ``app/alpaca_broker/client.py``, the only module allowed to name them.

Where a broker documents a feature AXIOM deliberately does not enable (F&O,
MTF, AMO, extended hours, notional orders, bracket/OCO/OTO, trailing stops) it
is listed in ``unsupported`` with the reason, so an operator can see the gap
instead of discovering it at submission time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from app.execution.contract import (
    BROKER_ALPACA,
    BROKER_UPSTOX,
    ExecutionOrderIntent,
    ExecutionRefused,
    OrderType,
    Product,
    TimeInForce,
)
from app.execution.sessions import SessionWindow

DOCUMENTED_AS_OF = "2026-11-07"

SOURCES: dict[str, tuple[str, ...]] = {
    BROKER_UPSTOX: (
        "https://upstox.com/developer/api-documentation/place-order/",
        "https://upstox.com/developer/api-documentation/order-details/",
        "https://upstox.com/developer/api-documentation/appendix/order-status/",
        "https://upstox.com/developer/api-documentation/announcements/enhanced-url/",
    ),
    BROKER_ALPACA: (
        "https://docs.alpaca.markets/reference/postorder",
        "https://docs.alpaca.markets/reference/getorderbyclientid",
        "https://docs.alpaca.markets/docs/orders-at-alpaca",
        "https://docs.alpaca.markets/docs/real-time-stock-pricing-data",
    ),
}


@dataclass(frozen=True)
class IdempotencySpec:
    """How a broker lets a client recognise its own order again.

    ``rejects_duplicates`` is True only when the broker itself refuses a second
    submission with the same key (Alpaca documents this for ``client_order_id``).
    Upstox documents ``tag`` as a client-chosen identifier that can be queried
    back, but **not** as a duplicate-rejecting key — so AXIOM must look the tag
    up before ever retrying, and must never assume the broker blocked a double
    submit.
    """

    field: str
    max_length: int
    query_by_key: str
    rejects_duplicates: bool
    documented: str


@dataclass(frozen=True)
class BrokerProfile:
    broker: str
    label: str
    market: str
    currency: str
    instrument_class: str
    instrument_identifier: str
    order_types: tuple[OrderType, ...]
    time_in_force: tuple[TimeInForce, ...]
    products: tuple[Product, ...]
    session: SessionWindow
    account_operations: tuple[str, ...]
    order_operations: tuple[str, ...]
    idempotency: IdempotencySpec
    unsupported: tuple[str, ...]
    notes: tuple[str, ...]
    sources: tuple[str, ...]
    documented_as_of: str = DOCUMENTED_AS_OF

    # -- capability checks -----------------------------------------------------

    def supports_order_type(self, order_type: OrderType) -> bool:
        return order_type in self.order_types

    def supports_time_in_force(self, time_in_force: TimeInForce) -> bool:
        return time_in_force in self.time_in_force

    def supports_product(self, product: Product) -> bool:
        return product in self.products

    def check(self, intent: ExecutionOrderIntent) -> None:
        """Raise ``ExecutionRefused`` unless this broker documents the request."""
        if intent.broker != self.broker:
            raise ExecutionRefused(
                "UNSUPPORTED_INSTRUMENT", f"intent targets {intent.broker}, adapter is {self.broker}"
            )
        if not self.supports_order_type(intent.order_type):
            raise ExecutionRefused(
                "UNSUPPORTED_ORDER_TYPE",
                f"{self.broker} does not accept {intent.order_type.value} in AXIOM "
                f"(documented: {', '.join(t.value for t in self.order_types)})",
            )
        if not self.supports_time_in_force(intent.time_in_force):
            raise ExecutionRefused(
                "UNSUPPORTED_ORDER_TYPE",
                f"{self.broker} does not accept {intent.time_in_force.value} in AXIOM "
                f"(documented: {', '.join(t.value for t in self.time_in_force)})",
            )
        if not self.supports_product(intent.product):
            raise ExecutionRefused(
                "UNSUPPORTED_ORDER_TYPE",
                f"{self.broker} does not accept product {intent.product.value} in AXIOM "
                f"(documented: {', '.join(p.value for p in self.products)})",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "broker": self.broker,
            "label": self.label,
            "market": self.market,
            "currency": self.currency,
            "instrument_class": self.instrument_class,
            "instrument_identifier": self.instrument_identifier,
            "order_types": [value.value for value in self.order_types],
            "time_in_force": [value.value for value in self.time_in_force],
            "products": [value.value for value in self.products],
            "session": self.session.as_dict(),
            "account_operations": list(self.account_operations),
            "order_operations": list(self.order_operations),
            "idempotency": {
                "field": self.idempotency.field,
                "max_length": self.idempotency.max_length,
                "query_by_key": self.idempotency.query_by_key,
                "rejects_duplicates": self.idempotency.rejects_duplicates,
                "documented": self.idempotency.documented,
            },
            "unsupported": list(self.unsupported),
            "notes": list(self.notes),
            "sources": list(self.sources),
            "documented_as_of": self.documented_as_of,
        }


_UPSTOX = BrokerProfile(
    broker=BROKER_UPSTOX,
    label="Upstox (NSE/BSE cash equity)",
    market="NSE",
    currency="INR",
    instrument_class="cash equity",
    instrument_identifier="instrument_key (e.g. NSE_EQ|INE002A01018)",
    order_types=(OrderType.MARKET, OrderType.LIMIT, OrderType.STOP, OrderType.STOP_MARKET),
    time_in_force=(TimeInForce.DAY, TimeInForce.IOC),
    products=(Product.INTRADAY, Product.DELIVERY),
    session=SessionWindow(
        label="NSE regular session",
        timezone="Asia/Kolkata",
        open_time=time(9, 15),
        close_time=time(15, 30),
    ),
    account_operations=(
        "GET /v2/user/profile",
        "GET /v2/user/get-funds-and-margin",
        "GET /v2/portfolio/short-term-positions",
    ),
    order_operations=(
        "POST /v2/order/place (api-hft.upstox.com)",
        "GET /v2/order/details (by order_id or tag)",
        "GET /v2/order/retrieve-all",
        "PUT /v2/order/modify",
        "DELETE /v2/order/cancel",
    ),
    idempotency=IdempotencySpec(
        field="tag",
        max_length=20,
        query_by_key="GET /v2/order/details?tag=<tag>",
        rejects_duplicates=False,
        documented="tag is a client-chosen order identifier; order details and history can be "
        "queried by order_id or tag. Upstox does not document duplicate rejection, so a retry "
        "is only safe after a tag lookup shows no order.",
    ),
    unsupported=(
        "derivatives (F&O) — documented by Upstox, not enabled in AXIOM",
        "MTF product — documented, not enabled in AXIOM",
        "cover orders / OCO — documented, not enabled in AXIOM",
        "GTD validity — documented, not enabled in AXIOM",
        "after-market orders (is_amo) — documented, not enabled: AXIOM requires an open session",
        "slice/multi-order — documented, not enabled in AXIOM",
    ),
    notes=(
        "Order and cancellation calls target the documented enhanced order host api-hft.upstox.com; "
        "authentication and every other call use api.upstox.com.",
        "Upstox order book rows expose traded_quantity, so a partially filled working order is "
        "detectable as status 'open'/'trigger pending' with traded_quantity > 0.",
    ),
    sources=SOURCES[BROKER_UPSTOX],
)


_ALPACA = BrokerProfile(
    broker=BROKER_ALPACA,
    label="Alpaca (US equities)",
    market="US",
    currency="USD",
    instrument_class="US listed equity",
    instrument_identifier="ticker symbol",
    order_types=(OrderType.MARKET, OrderType.LIMIT, OrderType.STOP, OrderType.STOP_MARKET),
    time_in_force=(TimeInForce.DAY, TimeInForce.IOC),
    products=(Product.DELIVERY,),
    session=SessionWindow(
        label="US regular session",
        timezone="America/New_York",
        open_time=time(9, 30),
        close_time=time(16, 0),
    ),
    account_operations=(
        "GET /v2/account",
        "GET /v2/clock",
        "GET /v2/positions",
        "GET /v2/account/activities",
    ),
    # Endpoint strings are deliberately NOT repeated here: the repository guard
    # confines the Alpaca order surface to app/alpaca_broker/client.py, which
    # documents the exact paths. Operations are named instead.
    order_operations=(
        "Create an Order (Trading API v2)",
        "List Orders",
        "Get an Order by Client Order Id",
        "Cancel an Order",
        "Replace an Order",
    ),
    idempotency=IdempotencySpec(
        field="client_order_id",
        max_length=128,
        query_by_key="Get an Order by Client Order Id (Alpaca Trading API v2)",
        rejects_duplicates=True,
        documented="client_order_id is a client-supplied unique identifier; Alpaca documents that "
        "a duplicate submission with the same client_order_id is rejected, and the order can be "
        "retrieved by that id.",
    ),
    unsupported=(
        "options — documented by Alpaca, not enabled in AXIOM",
        "crypto — documented, not enabled in AXIOM",
        "notional orders — documented, not enabled: AXIOM sizes by quantity",
        "trailing_stop orders — documented, not enabled in AXIOM",
        "bracket / OCO / OTO order classes — documented, not enabled in AXIOM",
        "extended_hours — documented for limit+day, not enabled in AXIOM",
        "fractional quantities — documented for market+day, not enabled in AXIOM",
        "FOK validity — documented, not enabled in AXIOM",
    ),
    notes=(
        "AXIOM's STOP type maps to Alpaca stop_limit (stop_price + limit_price); AXIOM's "
        "STOP_MARKET type maps to Alpaca 'stop'. Alpaca has no separate stop-limit flag.",
        "Alpaca documents a separate paper environment that is the official non-live environment; "
        "it is a different host from the live environment and fills at the broker, not on a real exchange. "
        "The exact hosts are pinned in app/alpaca_broker/client.py, the only module allowed to name them.",
    ),
    sources=SOURCES[BROKER_ALPACA],
)


PROFILES: dict[str, BrokerProfile] = {
    BROKER_UPSTOX: _UPSTOX,
    BROKER_ALPACA: _ALPACA,
}


def profile_for(broker: str) -> BrokerProfile:
    key = str(getattr(broker, "value", broker)).upper()
    try:
        return PROFILES[key]
    except KeyError as exc:
        raise ExecutionRefused("INVALID_INTENT", f"unknown broker {broker!r}") from exc


def describe_all() -> list[dict[str, object]]:
    return [profile.as_dict() for profile in PROFILES.values()]
