"""Alpaca Trading API client — the ONLY module that may reach order endpoints.

Environments are explicit and strictly separate:

- ``paper`` -> ``https://paper-api.alpaca.markets`` (Alpaca's official
  paper-trading environment; simulated fills at the broker, no real money)
- ``live``  -> ``https://api.alpaca.markets`` (real-money endpoints)

A LIVE client cannot be constructed without a :class:`LivePermit` capability
token that only :meth:`app.alpaca_broker.gate.BrokerLiveGate.activate` can mint
after the operator explicitly Arms and then Activates. Without the permit this
module raises :class:`LiveEnvironmentBlocked` BEFORE any network call. A LIVE
permit is never accepted on a PAPER client.

The market-data module (``app/data/alpaca.py``) remains order-free. Secrets are
never logged, repr'd, or included in exceptions; account numbers are masked.

Endpoints used (Alpaca Trading API v2, official docs 2026): ``GET /v2/account``,
``GET /v2/clock``, ``GET /v2/orders``, ``GET /v2/orders/{id}``,
``GET /v2/orders:by_client_order_id``, ``POST /v2/orders``,
``DELETE /v2/orders/{id}``, ``DELETE /v2/orders``, ``PATCH /v2/orders/{id}``,
``GET /v2/positions``, ``GET /v2/account/activities``. Nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging

import httpx

logger = logging.getLogger("bot.alpaca_broker")

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
_PAPER_BASE = f"https://{PAPER_HOST}"
_LIVE_BASE = f"https://{LIVE_HOST}"

_ALLOWED_PATHS = (
    "/v2/account",
    "/v2/clock",
    "/v2/orders",
    "/v2/orders:",
    "/v2/positions",
    "/v2/account/activities",
)


class BrokerError(RuntimeError):
    """Sanitized broker failure. Messages never include secrets or raw bodies."""


class BrokerRejected(BrokerError):
    """The broker refused an order/request and reported why."""


class LiveEnvironmentBlocked(BrokerError):
    """LIVE environment requested without an explicit, armed+activated permit."""


@dataclass(frozen=True)
class LivePermit:
    """Capability token for LIVE requests; minted only by BrokerLiveGate."""

    gate_id: str
    issued_at: str


def _mask_account(account_number) -> str:
    text = str(account_number or "")
    if len(text) <= 4:
        return "****"
    return "*" * (len(text) - 4) + text[-4:]


class AlpacaTradingClient:
    """Minimal read/write Trading API client for one explicit environment."""

    def __init__(
        self,
        key: str,
        secret: str,
        *,
        environment: str = "paper",
        live_permit: LivePermit | None = None,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ):
        if not isinstance(key, str) or not key.strip() or not isinstance(secret, str) or not secret.strip():
            raise ValueError("Alpaca broker credentials are required (broker key/secret, not market-data keys)")
        if environment not in {"paper", "live"}:
            raise ValueError("Alpaca broker environment must be 'paper' or 'live'")
        self.environment = environment
        if environment == "live":
            if not isinstance(live_permit, LivePermit):
                raise LiveEnvironmentBlocked(
                    "Alpaca LIVE is unavailable: no active LIVE permit. LIVE requires "
                    "all readiness checks to pass and an explicit operator Arm + "
                    "Activate. Use the paper environment otherwise."
                )
            self._base = _LIVE_BASE
            self.live_permit = live_permit
        else:
            if live_permit is not None:
                raise ValueError("a LIVE permit must never be attached to a PAPER client")
            self._base = _PAPER_BASE
            self.live_permit = None
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        if client is not None:
            self._client.headers.update(headers)
        self._owns_client = client is None

    # -- identity (safe to display) -------------------------------------------

    @property
    def endpoint_host(self) -> str:
        return PAPER_HOST if self.environment == "paper" else LIVE_HOST

    @property
    def mode(self) -> str:
        return "ALPACA_PAPER" if self.environment == "paper" else "ALPACA_LIVE"

    def __repr__(self) -> str:  # never include credentials
        return f"AlpacaTradingClient(environment={self.environment!r})"

    # -- request plumbing ------------------------------------------------------

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> httpx.Response:
        if not path.startswith("/v2/") or not any(
            path == allowed or path.startswith(allowed) for allowed in _ALLOWED_PATHS
        ):
            raise BrokerError("refusing non-Trading-API request path")
        response = self._client.request(
            method, f"{self._base}{path}", params=params, json=json_body,
        )
        if response.status_code == 429:
            raise BrokerError("Alpaca broker rate limit reached (HTTP 429); failing closed")
        return response

    @staticmethod
    def _fail(response: httpx.Response, action: str) -> None:
        if response.status_code < 400:
            return
        try:
            message = str(response.json().get("message") or "")
        except Exception:
            message = ""
        detail = message[:200] if message else f"HTTP {response.status_code}"
        if response.status_code == 422:
            raise BrokerRejected(f"Alpaca rejected {action}: {detail}")
        if response.status_code in {401, 403}:
            raise BrokerError(f"Alpaca refused {action} (HTTP {response.status_code}); verify broker credentials/permissions")
        raise BrokerError(f"Alpaca {action} failed: {detail}")

    # -- reads -----------------------------------------------------------------

    def get_account(self) -> dict:
        response = self._request("GET", "/v2/account")
        self._fail(response, "account read")
        raw = response.json()
        return {
            "id_masked": _mask_account(raw.get("id")),
            "account_number_masked": _mask_account(raw.get("account_number")),
            "status": raw.get("status"),
            "currency": raw.get("currency"),
            "cash": raw.get("cash"),
            "buying_power": raw.get("buying_power"),
            "equity": raw.get("equity"),
            "last_equity": raw.get("last_equity"),
            "portfolio_value": raw.get("portfolio_value"),
            "trading_blocked": bool(raw.get("trading_blocked")),
            "trade_suspended_by_user": bool(raw.get("trade_suspended_by_user")),
            "account_blocked": bool(raw.get("account_blocked")),
        }

    def get_clock(self) -> dict:
        response = self._request("GET", "/v2/clock")
        self._fail(response, "clock read")
        return response.json()

    def get_order(self, order_id: str) -> dict:
        response = self._request("GET", f"/v2/orders/{order_id}")
        self._fail(response, "order read")
        return response.json()

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        response = self._request(
            "GET", "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
        )
        if response.status_code == 404:
            return None
        self._fail(response, "order lookup")
        return response.json()

    def get_orders(self, *, status: str = "open", limit: int = 100) -> list[dict]:
        response = self._request("GET", "/v2/orders", params={"status": status, "limit": limit})
        self._fail(response, "orders read")
        return list(response.json())

    def get_positions(self) -> list[dict]:
        response = self._request("GET", "/v2/positions")
        self._fail(response, "positions read")
        return list(response.json())

    def get_fill_activities(self, *, after: str | None = None) -> list[dict]:
        params = {"type": "FILL"}
        if after:
            params["after"] = after
        response = self._request("GET", "/v2/account/activities", params=params)
        self._fail(response, "activities read")
        return list(response.json())

    # -- writes ----------------------------------------------------------------

    def submit_order(self, payload: dict) -> dict:
        """POST /v2/orders. ``client_order_id`` is mandatory (idempotency)."""
        body = dict(payload)
        if not body.get("client_order_id"):
            raise ValueError("client_order_id is mandatory for every broker order (duplicate prevention)")
        response = self._request("POST", "/v2/orders", json_body=body)
        self._fail(response, "order submission")
        return response.json()

    def cancel_order(self, order_id: str) -> None:
        response = self._request("DELETE", f"/v2/orders/{order_id}")
        if response.status_code == 404:
            raise BrokerRejected(f"Alpaca could not cancel order {order_id}: not found")
        self._fail(response, "order cancellation")

    def cancel_all(self) -> int:
        response = self._request("DELETE", "/v2/orders")
        self._fail(response, "cancel-all")
        try:
            return len(response.json() or [])
        except Exception:
            return 0

    def replace_order(self, order_id: str, fields: dict) -> dict:
        allowed = {"qty", "limit_price", "stop_price", "time_in_force"}
        body = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not body:
            raise ValueError("replace requires at least one of qty/limit_price/stop_price/time_in_force")
        response = self._request("PATCH", f"/v2/orders/{order_id}", json_body=body)
        self._fail(response, "order replace")
        return response.json()

    def close_position(self, symbol: str) -> dict:
        """DELETE /v2/positions/{symbol} — explicit operator-requested position close."""
        clean_symbol = (symbol or "").strip().upper()
        if not clean_symbol:
            raise ValueError("symbol is required to close a position")
        response = self._request("DELETE", f"/v2/positions/{clean_symbol}")
        if response.status_code == 404:
            raise BrokerRejected(f"Alpaca has no open position for {clean_symbol}")
        self._fail(response, "position close")
        return response.json()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class AlpacaReadOnlyClient:
    """Strictly GET-only Alpaca account/clock/order/position inspection client.

    Used for preflight readiness checks, connection verification, and recovery
    reconciliation before ``LivePermit`` activation. Has zero order-write,
    cancel, or replace methods and hard-refuses any HTTP verb other than GET.
    """

    def __init__(
        self,
        key: str,
        secret: str,
        *,
        environment: str = "paper",
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(key, str) or not key.strip() or not isinstance(secret, str) or not secret.strip():
            raise ValueError("Alpaca broker credentials are required for read-only inspection")
        if environment not in {"paper", "live"}:
            raise ValueError("Alpaca broker environment must be 'paper' or 'live'")
        self.environment = environment
        self._base = _PAPER_BASE if environment == "paper" else _LIVE_BASE
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        if client is not None:
            self._client.headers.update(headers)
        self._owns_client = client is None

    @property
    def endpoint_host(self) -> str:
        return PAPER_HOST if self.environment == "paper" else LIVE_HOST

    def __repr__(self) -> str:
        return f"AlpacaReadOnlyClient(environment={self.environment!r})"

    def _get(self, path: str, *, params=None) -> httpx.Response:
        if not path.startswith("/v2/") or not any(
            path == allowed or path.startswith(allowed) for allowed in _ALLOWED_PATHS
        ):
            raise BrokerError("refusing non-Trading-API request path")
        try:
            response = self._client.request("GET", f"{self._base}{path}", params=params)
        except httpx.HTTPError as exc:
            raise BrokerError(f"Alpaca read endpoint unreachable ({type(exc).__name__})") from None
        if response.status_code == 429:
            raise BrokerError("Alpaca broker rate limit reached (HTTP 429); failing closed")
        return response

    def get_account(self) -> dict:
        response = self._get("/v2/account")
        AlpacaTradingClient._fail(response, "account read")
        raw = response.json()
        return {
            "id_masked": _mask_account(raw.get("id")),
            "account_number_masked": _mask_account(raw.get("account_number")),
            "status": raw.get("status"),
            "currency": raw.get("currency"),
            "cash": raw.get("cash"),
            "buying_power": raw.get("buying_power"),
            "equity": raw.get("equity"),
            "last_equity": raw.get("last_equity"),
            "portfolio_value": raw.get("portfolio_value"),
            "trading_blocked": bool(raw.get("trading_blocked")),
            "trade_suspended_by_user": bool(raw.get("trade_suspended_by_user")),
            "account_blocked": bool(raw.get("account_blocked")),
        }

    def get_clock(self) -> dict:
        response = self._get("/v2/clock")
        AlpacaTradingClient._fail(response, "clock read")
        return response.json()

    def get_order(self, order_id: str) -> dict:
        response = self._get(f"/v2/orders/{order_id}")
        AlpacaTradingClient._fail(response, "order read")
        return response.json()

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        response = self._get(
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
        )
        if response.status_code == 404:
            return None
        AlpacaTradingClient._fail(response, "order lookup")
        return response.json()

    def get_orders(self, *, status: str = "open", limit: int = 100) -> list[dict]:
        response = self._get("/v2/orders", params={"status": status, "limit": limit})
        AlpacaTradingClient._fail(response, "orders read")
        return list(response.json())

    def get_positions(self) -> list[dict]:
        response = self._get("/v2/positions")
        AlpacaTradingClient._fail(response, "positions read")
        return list(response.json())

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
