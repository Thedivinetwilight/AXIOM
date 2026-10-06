"""A deterministic, in-process mock of the Upstox order API (FINAL-11/10 §56).

This is how the ENTIRE live architecture is exercised without an exchange:
the real :class:`UpstoxLiveBroker` / :class:`UpstoxSandboxBroker` speaks its
real wire protocol (place / cancel / order-history / tag lookup) to this
exchange through a test-only HTTPX adapter. The live broker rejects injected
caller-owned transports. No socket is opened in the test harness, so no
automated run can place a real order by construction.

The exchange is SCRIPTABLE and DETERMINISTIC. Every failure class the live
system must survive has an explicit injection point:

* ``fail_next_place("reject_400" | "rate_limit_429" | "server_500" |
  "network" | "auth_401")`` — semantic broker errors (§21);
* ``lose_next_place_response()`` — the order IS created server-side but the
  acknowledgement never reaches the client (accepted-but-response-lost,
  §22): only the documented tag lookup may resolve it;
* ``set_fill_plan(order_id, [..cumulative fills..])`` — partial and DELAYED
  fills revealed one step per history poll;
* ``fail_next_cancel(...)`` — cancel refused / lost, resolved through history;
* duplicate place calls carrying the same ``tag`` return the SAME order id
  (exchange-side idempotency, counted for assertions);
* ``fail_next_history(...)`` — reconciliation degraded without corruption.

Every request is appended to ``request_log`` (method, path, params) so tests
can prove WHAT reached the exchange and what did not.
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)

#: failure kinds understood by the injection points
PLACE_FAILURES = ("reject_400", "rate_limit_429", "server_500", "network",
                  "auth_401")


class MockUpstoxExchange:
    """Deterministic fake Upstox order endpoint (transport-level)."""

    def __init__(self, *, token: str = "mock-token"):
        self.token = token
        self.orders: dict[str, dict] = {}
        self.by_tag: dict[str, list[str]] = {}
        self.request_log: list[dict] = []
        self.duplicate_place_attempts = 0
        self._seq = 0
        self._ts = 0
        self._place_failures: list[str] = []
        self._lose_next_place_response = False
        self._history_failures: list[str] = []
        self._cancel_failures: list[str] = []
        self._auth_failures = 0

    # ---------------------------------------------------- fault injection --
    def fail_next_place(self, kind: str, times: int = 1) -> None:
        if kind not in PLACE_FAILURES:
            raise ValueError(f"unknown place failure {kind!r}")
        self._place_failures.extend([kind] * max(1, int(times)))

    def lose_next_place_response(self) -> None:
        """Create the order server-side, then drop the acknowledgement."""
        self._lose_next_place_response = True

    def fail_next_history(self, kind: str, times: int = 1) -> None:
        if kind not in ("server_500", "rate_limit_429", "network"):
            raise ValueError(f"unknown history failure {kind!r}")
        self._history_failures.extend([kind] * max(1, int(times)))

    def fail_next_cancel(self, kind: str, times: int = 1) -> None:
        if kind not in ("reject_400", "server_500", "network"):
            raise ValueError(f"unknown cancel failure {kind!r}")
        self._cancel_failures.extend([kind] * max(1, int(times)))

    def reject_next_auth(self, times: int = 1) -> None:
        self._auth_failures += max(1, int(times))

    def set_fill_plan(self, order_id: str, cumulative_fills) -> None:
        """Reveal cumulative filled quantities one step per history poll."""
        order = self.orders[order_id]
        plan = [float(f) for f in cumulative_fills]
        if any(f < 0 or f > order["quantity"] for f in plan):
            raise ValueError("fill plan outside [0, quantity]")
        if plan != sorted(plan):
            raise ValueError("fill plan must be non-decreasing")
        order["fill_plan"] = plan
        order["fill_step"] = 0

    def reject_order(self, order_id: str) -> None:
        self.orders[order_id]["status"] = "rejected"

    # ------------------------------------------------------------- state --
    def _next_order_id(self) -> str:
        self._seq += 1
        return f"MOCK-{self._seq:06d}"

    def _tick(self) -> str:
        self._ts += 1
        return f"2026-01-01 09:15:{self._ts:02d}"

    def _current_fill(self, order: dict) -> float:
        plan = order.get("fill_plan")
        if not plan:
            return float(order["quantity"]) if order["status"] == "complete" \
                else 0.0
        step = min(order.get("fill_step", 0), len(plan) - 1)
        return float(plan[step])

    def _advance_fill(self, order: dict) -> None:
        plan = order.get("fill_plan")
        if not plan or order["status"] in {"cancelled", "rejected"}:
            return
        step = order.get("fill_step", 0)
        filled = float(plan[min(step, len(plan) - 1)])
        order["fill_step"] = min(step + 1, len(plan) - 1)
        order["filled"] = filled
        order["avg_price"] = order["price"] or order.get("avg_price") or 100.0
        if filled >= order["quantity"]:
            order["status"] = "complete"
        elif filled > 0:
            order["status"] = "open"

    def _row(self, order: dict) -> dict:
        return {
            "order_id": order["order_id"],
            "tag": order["tag"],
            "transaction_type": order["transaction_type"],
            "quantity": int(order["quantity"]),
            "filled_quantity": float(order.get("filled", 0.0)),
            "average_traded_price": float(order.get("avg_price", 0.0)),
            "price": float(order.get("price", 0.0)),
            "order_type": order.get("order_type", "MARKET"),
            "instrument_token": order["instrument_token"],
            "product": order["product"],
            "status": order["status"],
            "order_timestamp": self._tick(),
        }

    # ------------------------------------------------------------- wiring --
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _json(self, status: int, payload: dict) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.request_log.append({"method": request.method, "path": path,
                                 "params": params})
        if self._auth_failures > 0:
            self._auth_failures -= 1
            return self._json(401, {"status": "error",
                                    "message": "unauthorized"})
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.token}":
            return self._json(401, {"status": "error",
                                    "message": "unauthorized"})
        if path.endswith("/v3/order/place") and request.method == "POST":
            return self._place(request)
        if path.endswith("/v3/order/cancel") and request.method == "DELETE":
            return self._cancel(params)
        if "order/history" in path and request.method == "GET":
            return self._history(params)
        if path.endswith("/v2/order/retrieve-all") and request.method == "GET":
            return self._order_book()
        if path.endswith("/v2/portfolio/short-term-positions") \
                and request.method == "GET":
            return self._positions()
        if path.endswith("/v2/portfolio/long-term-holdings") \
                and request.method == "GET":
            return self._json(200, {"status": "success", "data": []})
        if path.endswith("/user/profile") and request.method == "GET":
            return self._json(200, {"status": "success", "data": {
                "email": "mock@example.invalid", "name": "Mock Account"}})
        return self._json(404, {"status": "error", "message": "not found"})

    # ----------------------------------------------------------- handlers --
    def _place(self, request: httpx.Request) -> httpx.Response:
        if self._place_failures:
            kind = self._place_failures.pop(0)
            if kind == "reject_400":
                return self._json(400, {"status": "error",
                                        "message": "mock rejection"})
            if kind == "rate_limit_429":
                return self._json(429, {"status": "error",
                                        "message": "rate limited"})
            if kind == "server_500":
                return self._json(500, {"status": "error",
                                        "message": "provider error"})
            if kind == "auth_401":
                return self._json(401, {"status": "error",
                                        "message": "unauthorized"})
            raise httpx.ConnectError("mock network failure", request=request)
        try:
            body = json.loads(request.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"status": "error",
                                    "message": "unparseable body"})
        tag = str(body.get("tag") or "")
        if tag and self.by_tag.get(tag):
            # Exchange-side idempotency: the same client tag maps to the SAME
            # order. A blind resubmission cannot create a second position.
            self.duplicate_place_attempts += 1
            existing = self.by_tag[tag][0]
            return self._json(200, {"status": "success",
                                    "data": {"order_ids": [existing]}})
        order_id = self._next_order_id()
        quantity = float(body.get("quantity") or 0)
        order = {
            "order_id": order_id,
            "tag": tag,
            "transaction_type": str(body.get("transaction_type") or ""),
            "quantity": quantity,
            "price": float(body.get("price") or 0.0),
            "order_type": str(body.get("order_type") or "MARKET"),
            "instrument_token": str(body.get("instrument_token") or ""),
            "product": str(body.get("product") or "I"),
            "status": "open",
            "filled": 0.0,
            "avg_price": 0.0,
            "fill_plan": [quantity],   # default: fully filled on first poll
            "fill_step": 0,
        }
        self.orders[order_id] = order
        if tag:
            self.by_tag.setdefault(tag, []).append(order_id)
        if self._lose_next_place_response:
            # The order EXISTS server-side, but the acknowledgement is lost.
            # The client may only resolve this through the documented tag
            # lookup — exactly the §22 accepted-but-response-lost case.
            self._lose_next_place_response = False
            raise httpx.ConnectError("acknowledgement lost", request=request)
        return self._json(200, {"status": "success",
                                "data": {"order_ids": [order_id]}})

    def _cancel(self, params: dict) -> httpx.Response:
        if self._cancel_failures:
            kind = self._cancel_failures.pop(0)
            if kind == "reject_400":
                return self._json(400, {"status": "error",
                                        "message": "cannot cancel"})
            if kind == "server_500":
                return self._json(500, {"status": "error",
                                        "message": "provider error"})
            raise httpx.ConnectError("mock cancel network failure")
        order_id = str(params.get("order_id") or "")
        order = self.orders.get(order_id)
        if order is None:
            return self._json(400, {"status": "error",
                                    "message": "unknown order"})
        if order["status"] in {"complete", "cancelled", "rejected"}:
            # Documented: cancelling an already-final order is rejected; the
            # client must resolve the truth through order history.
            return self._json(400, {"status": "error",
                                    "message": "order already final"})
        order["status"] = "cancelled"
        return self._json(200, {"status": "success",
                                "data": {"order_id": order_id}})

    def _order_book(self) -> httpx.Response:
        rows = []
        for order in self.orders.values():
            row = self._row(order)
            filled = self._current_fill(order)
            terminal = str(order.get("status", "")).lower() in {
                "complete", "completed", "rejected", "cancelled", "canceled"}
            row["pending_quantity"] = (0.0 if terminal else max(
                0.0, float(order["quantity"]) - float(filled)))
            rows.append(row)
        return self._json(200, {"status": "success", "data": rows})

    def _positions(self) -> httpx.Response:
        positions = {}
        for order in self.orders.values():
            filled = float(self._current_fill(order))
            sign = 1.0 if order.get("transaction_type") == "BUY" else -1.0
            key = (order.get("instrument_token"), order.get("product"))
            current = positions.setdefault(key, {"quantity": 0.0, "price": 0.0})
            current["quantity"] += sign * filled
            current["price"] = float(
                order.get("avg_price") or order.get("price") or 100.0)
        rows = [{"instrument_token": instrument, "product": product,
                 "quantity": values["quantity"],
                 "average_price": values["price"],
                 "last_price": values["price"],
                 "value": values["quantity"] * values["price"]}
                for (instrument, product), values in positions.items()
                if abs(values["quantity"]) > 1e-9]
        return self._json(200, {"status": "success", "data": rows})

    def _history(self, params: dict) -> httpx.Response:
        if self._history_failures:
            kind = self._history_failures.pop(0)
            if kind in ("server_500", "rate_limit_429"):
                return self._json(500 if kind == "server_500" else 429,
                                  {"status": "error", "message": "degraded"})
            raise httpx.ConnectError("mock history network failure")
        order_id = str(params.get("order_id") or "")
        tag = str(params.get("tag") or "")
        rows: list[dict] = []
        if order_id:
            order = self.orders.get(order_id)
            if order is not None:
                self._advance_fill(order)
                rows = [self._row(order)]
        elif tag:
            for oid in self.by_tag.get(tag, []):
                order = self.orders[oid]
                self._advance_fill(order)
                rows.append(self._row(order))
        return self._json(200, {"status": "success", "data": rows})


__all__ = ["MockUpstoxExchange", "PLACE_FAILURES"]
