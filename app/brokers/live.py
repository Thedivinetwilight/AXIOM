"""Upstox LIVE (real-money) broker — AXIOM's first-party live broker path.

This application boundary does not control arbitrary HTTP calls made outside
AXIOM by another process or by in-process code that already holds credentials.

This subclass exists so that real-money routing is (a) reachable at all, and
(b) *always truthfully labelled*. In V1 the sandbox broker defaulted to the live
order host, so real orders could be placed while the broker, the audit tables,
the dashboard and the reports all said "SANDBOX" / "live_trading: false".

Design rules enforced here:
- ONLY a documented live order host may be used (``api-hft.upstox.com``);
  sandbox and auth/read hosts are refused at construction.
- ``MODE`` / ``LIVE`` / ``repr`` / ``status_view`` state the truth, so an
  operator reading logs, the API or the database cannot be misled.
- Orders and transitions are written to the dedicated ``live_orders`` /
  ``live_transitions`` tables, never into the sandbox tables.
- Construction ALSO demands the exact documented ``LIVE_CONFIRM_PHRASE``, so a
  config mistake cannot instantiate a live broker by accident.

The four-switch configuration gate (MODE=LIVE + LIVE_TRADING=true +
BROKER=UPSTOX_LIVE + LIVE_CONFIRM) and the non-zero risk-limit requirement live
in ``app/config/settings.py`` and are validated before this class is reachable
from the CLI.
"""

import hashlib
import logging
import threading
from contextvars import ContextVar
from weakref import WeakKeyDictionary, ref

from app.brokers.sandbox import SandboxLedger, SandboxRequestError, UpstoxSandboxBroker
from app.config.hosts import (
    DEFAULT_LIVE_BASE_URL,
    DEFAULT_ORDER_HISTORY_URL,
    FORBIDDEN_IN_LIVE,
    LIVE_ORDER_HOSTS,
    host_of,
)
from app.config.settings import LIVE_CONFIRM_PHRASE

logger = logging.getLogger("bot.live")

# The gateway binding and active intent live off the broker instance. There is
# deliberately no instance-level ``authorized`` flag or public method that can
# bind an arbitrary caller-supplied token. A mutation scope is created only by
# the concrete broker after it receives the capability + immutable intent that
# the LiveExecutionGateway supplied.
_CAPABILITY_LOCK = threading.RLock()
_BROKER_CAPABILITIES = WeakKeyDictionary()  # broker -> (gateway, opaque capability)
_WRITE_SCOPE = ContextVar("axiom_live_write_scope", default=frozenset())
_WRITE_AUTHORIZATIONS = ContextVar("axiom_live_write_authorizations", default=())
_REQUEST_AUTHORIZATION = ContextVar("axiom_live_request_authorization", default=None)


def _bind_live_gateway(gateway, broker, capability) -> bool:
    """Bind one gateway to one concrete live broker; never bind arbitrary state.

    The gateway first registers this exact capability in its private registry.
    The broker holds the binding only in a weak-key module registry, not as a
    mutable instance flag/token. Non-production broker doubles return False.
    """
    if not isinstance(broker, UpstoxLiveBroker):
        return False
    if capability is None or getattr(gateway, "broker", None) is not broker:
        raise ValueError("live broker capability must be bound by its owning gateway")
    from app.live.gateway import _gateway_capability

    if _gateway_capability(gateway) is not capability:
        raise ValueError("live broker capability was not issued to this gateway")
    with _CAPABILITY_LOCK:
        existing = _BROKER_CAPABILITIES.get(broker)
        if existing is not None:
            existing_gateway = existing[0]()
            if existing_gateway is not None and (
                    existing_gateway is not gateway or existing[1] is not capability):
                raise RuntimeError("live broker is already bound to another gateway")
        _BROKER_CAPABILITIES[broker] = (ref(gateway), capability)
    return True


def _binding_for(broker):
    with _CAPABILITY_LOCK:
        existing = _BROKER_CAPABILITIES.get(broker)
    if existing is None:
        return None
    gateway = existing[0]()
    return (gateway, existing[1]) if gateway is not None else None


def _write_authorization_for(broker):
    broker_id = id(broker)
    for entry in reversed(_WRITE_AUTHORIZATIONS.get()):
        if entry[0] == broker_id:
            return entry[1:]
    return None


def _enter_gateway_write_scope(broker, capability, intent):
    binding = _binding_for(broker)
    if binding is None or capability is None or binding[1] is not capability:
        from app.brokers.sandbox import SandboxRequestError

        raise SandboxRequestError(
            "Live order mutations are available only through the execution gateway")
    from app.brokers.sandbox import SandboxRequestError
    from app.live.gateway import ExecutionIntent

    if not isinstance(intent, ExecutionIntent):
        raise SandboxRequestError(
            "Live order mutations require an immutable gateway execution intent")
    problems = intent.validate()
    if problems:
        raise SandboxRequestError(
            "Live order mutations require a complete gateway execution intent")
    gateway = binding[0]
    entry = (id(broker), gateway, capability, intent)
    scope_token = _WRITE_SCOPE.set(_WRITE_SCOPE.get() | {id(broker)})
    authorization_token = _WRITE_AUTHORIZATIONS.set(
        _WRITE_AUTHORIZATIONS.get() + (entry,))
    return scope_token, authorization_token


def _leave_gateway_write_scope(token) -> None:
    if token is not None:
        scope_token, authorization_token = token
        _WRITE_AUTHORIZATIONS.reset(authorization_token)
        _WRITE_SCOPE.reset(scope_token)


def _in_gateway_write_scope(broker) -> bool:
    return id(broker) in _WRITE_SCOPE.get()


class UpstoxLiveBroker(UpstoxSandboxBroker):
    """REAL-MONEY Upstox order client (place/modify/cancel at the live host).

    Same audited, idempotent reconciliation machinery as the sandbox broker —
    different host, different audit tables, and a hard requirement that the
    operator has explicitly confirmed real-money routing.
    """

    MODE = "UPSTOX_LIVE"
    LIVE = True
    ALLOWED_HOSTS = LIVE_ORDER_HOSTS
    FORBIDDEN_HOSTS = FORBIDDEN_IN_LIVE
    DEFAULT_BASE_URL = DEFAULT_LIVE_BASE_URL
    READ_API_BASE_URL = "https://api.upstox.com"
    POSITIONS_PATH = "/v2/portfolio/short-term-positions"
    HOLDINGS_PATH = "/v2/portfolio/long-term-holdings"
    OPEN_ORDERS_PATH = "/v2/order/retrieve-all"

    def __init__(
        self,
        access_token: str,
        instrument_key: str,
        *,
        confirm: str = "",
        base_url: str = DEFAULT_LIVE_BASE_URL,
        history_url: str = DEFAULT_ORDER_HISTORY_URL,
        product: str = "I",
        tag: str = "arena-live",
        repository=None,
        initial_capital: float = 100_000.0,
        http_timeout: float = 20.0,
        transport=None,
        algo_name: str = "",
    ):
        if str(confirm) != LIVE_CONFIRM_PHRASE:
            # Defense in depth (FINAL-11/10 §16): the broker boundary demands
            # the EXACT phrase — not "non-empty", not case-insensitive, no
            # whitespace forgiveness. A config mistake cannot instantiate a
            # live broker.
            raise ValueError(
                "UpstoxLiveBroker places REAL orders and requires the EXACT "
                f"confirmation phrase (LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}); "
                "the supplied value does not match it precisely"
            )
        # A caller-owned transport remains usable through any reference the
        # caller retained before construction, which would bypass this broker's
        # final gateway authorization. The live client therefore never accepts
        # injected transports; tests patch httpx.request at the test boundary.
        if transport is not None:
            raise ValueError(
                "UpstoxLiveBroker refuses caller-owned HTTP transports; "
                "live requests must use its gateway-checked HTTP core")
        super().__init__(
            access_token, instrument_key,
            base_url=base_url, history_url=history_url, product=product, tag=tag,
            repository=repository, initial_capital=initial_capital,
            http_timeout=http_timeout, transport=None, algo_name=algo_name,
        )
        # Opaque credential fingerprint binds gateway intents to the concrete
        # authenticated account without persisting or logging the bearer token.
        self.account_scope = "upstox-live:" + hashlib.sha256(
            str(access_token).encode("utf-8")).hexdigest()
        # Truthful, unmissable startup record. Never contains the token.
        logger.warning(
            "LIVE_BROKER_READY",
            extra={"instrument_key": self.instrument_key,
                   "base_url": self._base_url,
                   "product": self._product, "tag": self._tag},
        )

    # -- gateway-only mutation boundary ------------------------------------

    def _begin_gateway_write(self, capability, intent):
        return _enter_gateway_write_scope(self, capability, intent)

    def _end_gateway_write(self, token) -> None:
        _leave_gateway_write_scope(token)

    def _authorize_request(self, method: str, url: str, **kwargs) -> None:
        """Require an active gateway intent for every non-safe HTTP method.

        This hook is reached by the base HTTP core. Live brokers reject
        caller-owned transports, so direct base-class calls still dispatch this
        authorization hook before the default HTTP client can open a socket.
        """
        method = str(method).upper()
        super()._authorize_request(method, url, **kwargs)
        if method in {"GET", "HEAD", "OPTIONS"}:
            return
        if not _in_gateway_write_scope(self):
            raise SandboxRequestError(
                "Live order mutations are available only through the execution gateway")
        authorization = _write_authorization_for(self)
        binding = _binding_for(self)
        if authorization is None or binding is None:
            raise SandboxRequestError(
                "Live order mutations are available only through the execution gateway")
        gateway, capability, intent = authorization
        if binding != (gateway, capability):
            raise SandboxRequestError(
                "Live order mutations are available only through the execution gateway")

        request_authorization = _REQUEST_AUTHORIZATION.get()
        request_scope = (id(self), method, str(url), id(gateway),
                         id(intent), id(capability))
        if request_authorization == request_scope:
            # This exact method+URL already passed final gateway revalidation
            # at entry to UpstoxLiveBroker._request; do not repeat the expensive
            # broker position/readiness checks in the base HTTP core.
            return
        gateway._authorize_broker_mutation(
            self, intent, method, str(url), capability=capability)

    def _request(self, method: str, url: str, **kwargs):
        """Revalidate at the actual mutating HTTP boundary, not just at submit()."""
        method = str(method).upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            return super()._request(method, url, **kwargs)
        # The inherited broker submit path writes journal/local state between
        # gateway.submit() and this HTTP call. Re-read all live authorization
        # here, after those writes and immediately before the network request.
        self._authorize_request(method, url, **kwargs)
        authorization = _write_authorization_for(self)
        if authorization is None:
            raise SandboxRequestError(
                "Live order mutations are available only through the execution gateway")
        gateway, capability, intent = authorization
        request_scope = (id(self), method, str(url), id(gateway),
                         id(intent), id(capability))
        request_token = _REQUEST_AUTHORIZATION.set(request_scope)
        try:
            return super()._request(method, url, **kwargs)
        finally:
            _REQUEST_AUTHORIZATION.reset(request_token)

    @staticmethod
    def _validate_submit_intent(broker, intent, *, side, quantity, price,
                                order_type, intent_id, symbol, instrument_key):
        from app.live.gateway import ExecutionIntent

        if not isinstance(intent, ExecutionIntent):
            raise SandboxRequestError(
                "Live order mutations are available only through the execution gateway "
                "with an immutable gateway execution intent")
        problems = intent.validate()
        if problems:
            raise SandboxRequestError(
                "Live order mutations are available only through the execution gateway "
                "with a complete execution intent")
        try:
            resolved_key = broker.resolve_instrument_key(symbol, instrument_key)
            matches = (
                intent.side == side
                and float(intent.quantity) == float(quantity)
                and intent.order_type.upper() == str(order_type).upper()
                and float(intent.price if intent.price is not None else 0.0)
                    == float(price)
                and intent.intent_id == str(intent_id or "")
                and intent.identity == resolved_key
                and intent.account_scope == broker.account_scope)
        except (TypeError, ValueError, OverflowError, AttributeError):
            matches = False
        expected_purpose = (
            intent.purpose == "ENTRY" if side == "BUY" else
            intent.purpose in {"EXIT", "RISK_REDUCE"} if side == "SELL" else False)
        if not matches or not expected_purpose:
            raise SandboxRequestError(
                "live broker arguments do not match the execution gateway intent")

    def submit(self, side: str, quantity: float, price: float, *,
               order_type: str = "MARKET", intent_id: str | None = None,
               symbol: str | None = None, instrument_key: str | None = None,
               **extra) -> str:
        capability = extra.pop("_gateway_capability", None)
        intent = extra.pop("_gateway_intent", None)
        self._validate_submit_intent(
            self, intent, side=side, quantity=quantity, price=price,
            order_type=order_type, intent_id=intent_id, symbol=symbol,
            instrument_key=instrument_key)
        token = self._begin_gateway_write(capability, intent)
        try:
            return super().submit(
                side, quantity, price, order_type=order_type,
                intent_id=intent_id, symbol=symbol,
                instrument_key=instrument_key, **extra)
        finally:
            self._end_gateway_write(token)

    def cancel(self, intent_id: str, *, _gateway_capability=None,
               _gateway_intent=None) -> dict:
        from app.live.gateway import ExecutionIntent

        state = self.orders.get(str(intent_id))
        try:
            state_identity = (
                state.symbol if state is not None and isinstance(state.symbol, str)
                and "|" in state.symbol else
                self.resolve_instrument_key(state.symbol, None)
                if state is not None else "")
        except (TypeError, ValueError):
            state_identity = ""
        intent_matches = False
        if isinstance(_gateway_intent, ExecutionIntent) and state is not None:
            try:
                intent_matches = (
                    _gateway_intent.purpose == "CANCEL"
                    and _gateway_intent.cancel_order_id == str(intent_id)
                    and _gateway_intent.account_scope == self.account_scope
                    and _gateway_intent.identity == state_identity
                    and _gateway_intent.side == state.side
                    and float(_gateway_intent.quantity) == float(state.quantity))
            except (TypeError, ValueError, OverflowError):
                intent_matches = False
        if not intent_matches:
            raise SandboxRequestError(
                "live cancellation is available only through the execution gateway "
                "with an intent matching the tracked order")
        token = self._begin_gateway_write(_gateway_capability, _gateway_intent)
        try:
            return super().cancel(intent_id)
        finally:
            self._end_gateway_write(token)

    def modify(self, intent_id: str, *, quantity: float | None = None,
               price: float | None = None, trigger_price: float | None = None,
               order_type: str | None = None, _gateway_capability=None,
               _gateway_intent=None) -> dict:
        from app.live.gateway import ExecutionIntent

        if (not isinstance(_gateway_intent, ExecutionIntent)
                or _gateway_intent.account_scope != self.account_scope):
            raise SandboxRequestError(
                "live modification is available only through the execution gateway "
                "with an authorized intent")
        token = self._begin_gateway_write(_gateway_capability, _gateway_intent)
        try:
            return super().modify(
                intent_id, quantity=quantity, price=price,
                trigger_price=trigger_price, order_type=order_type)
        finally:
            self._end_gateway_write(token)

    # -- audit routing: real money never lands in the sandbox tables --------

    def _record_order(self, record: dict) -> None:
        if self.repository is None:
            return
        self.repository.record_live_order(record)

    def _record_transition(self, order_id: str, upstox_order_id: str | None,
                           status: str, filled_quantity: float | None,
                           average_price: float | None, ts: str, raw: dict) -> bool:
        if self.repository is None:
            return True
        return self.repository.record_live_transition(
            order_id, upstox_order_id, status, filled_quantity, average_price, ts, raw)

    def _read_live_data(self, path: str, *, operation: str) -> list[dict]:
        """Read one documented live portfolio/order endpoint without guessing."""
        response = self._request(
            "GET", f"{self.READ_API_BASE_URL}{path}")
        if response.status_code != 200:
            raise SandboxRequestError(
                f"Live {operation} read failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception as exc:
            raise SandboxRequestError(
                f"Live {operation} response was not valid JSON") from exc
        if (not isinstance(payload, dict) or payload.get("status") != "success"
                or not isinstance(payload.get("data"), list)
                or any(not isinstance(row, dict) for row in payload["data"])):
            raise SandboxRequestError(
                f"Live {operation} response has an invalid shape")
        return payload["data"]

    def get_positions(self) -> list[dict]:
        """Return broker-confirmed net positions for the configured product.

        Intraday/MTF positions come from Upstox short-term positions. Overnight
        delivery holdings are queried separately because they do not appear in
        that endpoint; an unreadable endpoint refuses exits rather than
        assuming a position is flat or long.
        """
        rows = self._read_live_data(self.POSITIONS_PATH, operation="positions")
        normalized = []
        for row in rows:
            instrument = row.get("instrument_token")
            product = row.get("product")
            quantity = row.get("quantity")
            if (not isinstance(instrument, str) or not instrument.strip()
                    or not isinstance(product, str) or not product.strip()
                    or isinstance(quantity, bool)
                    or not isinstance(quantity, (int, float, str))):
                raise SandboxRequestError(
                    "Live positions contain a malformed instrument/product/quantity")
            normalized.append({**row, "instrument_token": instrument,
                               "product": product, "quantity": quantity})
        if self._product == "D":
            holdings = self._read_live_data(self.HOLDINGS_PATH,
                                            operation="delivery holdings")
            for row in holdings:
                instrument = row.get("instrument_token")
                quantity = row.get("quantity")
                if (not isinstance(instrument, str) or not instrument.strip()
                        or isinstance(quantity, bool)
                        or not isinstance(quantity, (int, float, str))):
                    raise SandboxRequestError(
                        "Live holdings contain a malformed instrument/quantity")
                normalized.append({**row, "instrument_token": instrument,
                                   "product": "D", "quantity": quantity})
        return normalized

    def get_account_positions(self) -> list[dict]:
        """Return both intraday positions and overnight delivery holdings.

        Position-level SELL authorization is product-scoped in
        ``get_positions``. New-entry exposure limits are account-scoped and
        must also see overnight holdings even when the configured order
        product is intraday.
        """
        rows = self._read_live_data(self.POSITIONS_PATH,
                                    operation="account positions")
        holdings = self._read_live_data(self.HOLDINGS_PATH,
                                        operation="account holdings")
        normalized = []
        for row in rows:
            instrument = row.get("instrument_token")
            product = row.get("product")
            quantity = row.get("quantity")
            if (not isinstance(instrument, str) or not instrument.strip()
                    or not isinstance(product, str) or not product.strip()
                    or isinstance(quantity, bool)
                    or not isinstance(quantity, (int, float, str))):
                raise SandboxRequestError(
                    "Live account positions contain malformed identity/quantity")
            normalized.append({**row, "instrument_token": instrument,
                               "product": product, "quantity": quantity})
        for row in holdings:
            instrument = row.get("instrument_token")
            quantity = row.get("quantity")
            if (not isinstance(instrument, str) or not instrument.strip()
                    or isinstance(quantity, bool)
                    or not isinstance(quantity, (int, float, str))):
                raise SandboxRequestError(
                    "Live account holdings contain malformed identity/quantity")
            normalized.append({**row, "instrument_token": instrument,
                               "product": "D", "quantity": quantity})
        return normalized

    def get_open_orders(self) -> list[dict]:
        """Return current-day remote orders with remaining quantities."""
        rows = self._read_live_data(self.OPEN_ORDERS_PATH,
                                    operation="open orders")
        normalized = []
        for row in rows:
            instrument = row.get("instrument_token")
            product = row.get("product")
            side = row.get("transaction_type")
            status = row.get("status")
            if (not isinstance(instrument, str) or not instrument.strip()
                    or not isinstance(product, str) or not product.strip()
                    or side not in {"BUY", "SELL"}
                    or not isinstance(status, str) or not status.strip()):
                raise SandboxRequestError(
                    "Live order book contains malformed identity/status")
            pending = row.get("pending_quantity")
            if pending is None:
                total = row.get("quantity")
                filled = row.get("filled_quantity")
                if (isinstance(total, (int, float)) and not isinstance(total, bool)
                        and isinstance(filled, (int, float))
                        and not isinstance(filled, bool)):
                    pending = float(total) - float(filled)
            normalized.append({**row, "instrument_token": instrument,
                               "product": product, "transaction_type": side,
                               "status": status,
                               "pending_quantity": pending})
        return normalized

    def status_view(self) -> dict:
        view = super().status_view()
        view.update(live=True, mode=self.MODE,
                    warning="REAL MONEY: live Upstox order host")
        return view


def live_broker_settings_ok(settings) -> tuple[bool, str]:
    """Read-only summary used by preflight output (never constructs a broker)."""
    host = host_of(settings.upstox_live_base_url)
    ok = (
        settings.broker.upper() == "UPSTOX_LIVE"
        and host in LIVE_ORDER_HOSTS
        and bool(settings.upstox_live_access_token.strip())
    )
    return ok, f"broker={settings.broker.upper()} host={host}"

# SandboxLedger is re-exported so callers that only import from this module can
# still build a ledger in tests; keeps the live surface self-describing.
__all__ = ["SandboxLedger", "UpstoxLiveBroker", "live_broker_settings_ok"]
