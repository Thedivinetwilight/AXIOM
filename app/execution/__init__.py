"""Shared execution contract for AXIOM's two live brokers (Upstox/NSE, Alpaca/US).

This package is *only* about the live execution boundary. It does not touch the
local paper engine, and by default it cannot reach a broker at all:

* :mod:`app.execution.profiles` records, per broker, exactly what the official
  documentation says that broker supports — instruments, order types, time in
  force, products, sessions, account operations and the documented idempotency
  mechanism. Nothing is inferred and the two brokers are not modelled as
  equivalent.
* :mod:`app.execution.contract` defines the order intent, the durable lifecycle
  states and the refusal codes shared by every adapter.
* :mod:`app.execution.store` persists an intent **before** any submission and
  keeps an append-only event log.
* :mod:`app.execution.limits` holds the mandatory, fail-closed risk limits.
* :mod:`app.execution.service` orchestrates gate -> readiness -> limits ->
  intent -> submit -> acknowledge -> reconcile, and stops for operator review
  whenever a submit result is ambiguous.
* :mod:`app.execution.upstox_adapter` and :mod:`app.execution.alpaca_adapter`
  speak to their own broker only, over an injectable transport.

Live execution is off by default, requires an explicit operator activation for
one broker *and* one account, and is refused outright whenever any limit,
session, readiness or reconciliation condition is unmet.
"""

from app.execution.contract import (
    BROKER_ALPACA,
    BROKER_UPSTOX,
    ExecutionOrderIntent,
    ExecutionRefused,
    OrderState,
    OrderSnapshot,
    Side,
    TimeInForce,
    OrderType,
    Product,
)

__all__ = [
    "BROKER_ALPACA",
    "BROKER_UPSTOX",
    "ExecutionOrderIntent",
    "ExecutionRefused",
    "OrderState",
    "OrderSnapshot",
    "Side",
    "TimeInForce",
    "OrderType",
    "Product",
]
