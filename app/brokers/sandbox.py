"""Upstox SANDBOX broker — documented sandbox order APIs only (V1, host-fixed V2).

HOST CLASSIFICATION (V2 live-hardening, 2026-10-02)
---------------------------------------------------
Upstox's live order host is ``api-hft.upstox.com`` ("enhanced URL for orders");
the sandbox base is ``api-sandbox.upstox.com`` (SDK ``Configuration(sandbox=True)``).
V1 shipped the LIVE host as the default ``UPSTOX_SANDBOX_BASE_URL`` and listed it
as a *sandbox* host, so a live token pasted into the sandbox variable placed real
orders while every label said "sandbox". That is fixed here:

* the default sandbox base URL is a sandbox host;
* ``api-hft.upstox.com`` is **refused at construction** by this class;
* the live host is reachable only through ``app.brokers.live.UpstoxLiveBroker``,
  which requires the full four-switch opt-in validated in ``Settings``.

CURRENT documentation verified 2026-09-22 (V1 implementation):
- Place Order V3 (sandbox-enabled): POST {base}/v3/order/place
  JSON body: quantity, product, validity, price, tag, instrument_token,
  order_type, transaction_type, disclosed_quantity, trigger_price, is_amo,
  slice, market_protection. Response: {"status":"success",
  "data":{"order_ids":[…]}, "metadata":{"latency":…}}.
- Cancel Order V3 (sandbox-enabled): DELETE {base}/v3/order/cancel?order_id=…
  Response: {"data":{"order_id":…}}. Already-finalized orders are rejected
  (documented UDAPI100040) — treated as already-closed, never fatal.
- Modify Order V3 (sandbox-enabled): PUT {base}/v3/order/modify.
- Reconciliation: GET {history_url}?order_id=… (documented `GET /order/history`
  on api.upstox.com/v2; default https://api.upstox.com/v2/order/history) returns
  the full ordered list of
  status transitions (…, "open", "complete"/"rejected") including
  filled_quantity and average_price. Orders are retained for one trading day.
  NOTE: sandbox order history is not part of the documented sandbox API surface
  (sandbox currently supports place/modify/cancel), so sandbox reconciliation
  against the production history endpoint may legitimately return no rows.
- Documented operational caveats surfaced honestly: sandbox tokens are for
  sandbox orders only; order APIs can be static-IP restricted (UDAPI1154);
  order placement may require an exchange-approved algo name header
  (X-Algo-Name, optional).

SAFETY:
- This broker talks ONLY to the configured sandbox base URL with the dedicated
  UPSTOX_SANDBOX_ACCESS_TOKEN. Live hosts are refused at construction; the
  real-money subclass lives in ``app.brokers.live``.
- Fails closed on auth errors (token rejected → broker disabled until
  re-created) and records every transition for audit/reconciliation.
- All requests inject the Bearer token per request; the token is never
  logged, printed, or embedded in exceptions.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from app.config.hosts import (
    DEFAULT_ORDER_HISTORY_URL,
    DEFAULT_SANDBOX_BASE_URL,
    FORBIDDEN_IN_SANDBOX,
    SANDBOX_ORDER_HOSTS,
    host_of,
)

logger = logging.getLogger("bot.sandbox")

#: Hosts this broker is permitted to send an order to.
#:
#: ``PLACE_PATH`` / ``CANCEL_PATH`` / ``MODIFY_PATH`` are real order-mutating
#: endpoints, and ``base_url`` is operator-supplied via
#: ``UPSTOX_SANDBOX_BASE_URL``. A single typo there must not be able to reach a
#: production host, so anything outside this set is refused at construction
#: rather than logged and allowed to proceed.
SANDBOX_ALLOWED_HOSTS = SANDBOX_ORDER_HOSTS

#: Hosts that must never receive a sandbox order, even if explicitly
#: allow-listed. This is a second, non-overridable gate: the order APIs are the
#: dangerous surface, so no caller — including a test — may opt a live or
#: auth/production host into the sandbox broker.
#:
#: ``api-hft.upstox.com`` is Upstox's LIVE order host and is FORBIDDEN here.
#: ``api.upstox.com`` serves auth/read APIs (a sandbox token 401s there).
SANDBOX_FORBIDDEN_HOSTS = FORBIDDEN_IN_SANDBOX



class SandboxAuthError(RuntimeError):
    """Sandbox credential rejected; the broker fails closed."""


class SandboxRequestError(RuntimeError):
    """Sandbox API failure (network/HTTP). Messages never contain the token."""


@dataclass
class SandboxLedger:
    """Transparent local accounting mirror of reconciled sandbox fills."""

    initial_capital: float
    cash: float = 0.0
    position_quantity: float = 0.0
    position_avg_price: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    mark: float = 0.0

    def __post_init__(self):
        if self.cash == 0.0 and self.position_quantity == 0.0:
            self.cash = float(self.initial_capital)

    def apply_fill(self, side: str, quantity: float, price: float, fees: float = 0.0) -> None:
        quantity, price = float(quantity), float(price)
        self.fees += float(fees)
        if side == "BUY":
            total_cost = quantity * price + fees
            self.cash -= total_cost
            new_quantity = self.position_quantity + quantity
            if new_quantity > 0:
                self.position_avg_price = (
                    (self.position_avg_price * self.position_quantity + quantity * price)
                    / new_quantity
                )
            self.position_quantity = new_quantity
        else:
            proceeds = quantity * price - fees
            self.cash += proceeds
            realized = (price - self.position_avg_price) * quantity - fees
            self.realized_pnl += realized
            self.position_quantity -= quantity
            if self.position_quantity <= 1e-12:
                self.position_quantity, self.position_avg_price = 0.0, 0.0

    def unrealized(self) -> float:
        if self.position_quantity <= 0:
            return 0.0
        return (self.mark - self.position_avg_price) * self.position_quantity

    def equity(self) -> float:
        return self.cash + self.position_quantity * (self.mark or self.position_avg_price)

    def snapshot(self) -> dict:
        return {
            "cash": self.cash,
            "position_quantity": self.position_quantity,
            "position_avg_price": self.position_avg_price,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized(),
            "fees": self.fees,
            "equity": self.equity(),
            "mark": self.mark,
        }


@dataclass
class SandboxOrderState:
    our_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    status: str = "PENDING_SUBMIT"
    upstox_order_ids: list = field(default_factory=list)
    filled_quantity: float = 0.0
    # 3.0.2: per-child (slice) remote status; the logical order only becomes
    # CANCELLED when EVERY child is confirmed cancelled by the broker.
    child_status: dict = field(default_factory=dict)
    # 3.0.3: per-child accounting baseline — each child's cumulative filled
    # quantity and the cumulative average price at that baseline. Logical
    # filled quantity is the SUM over children; new fills are applied as
    # per-child deltas priced at the INCREMENTAL basis, never by re-applying
    # a cumulative average to a whole delta.
    child_filled: dict = field(default_factory=dict)
    child_avg_price: dict = field(default_factory=dict)


def classify_history_probe(response) -> dict:
    """Classify a documented order-history probe response (3.0.1).

    A probe response counts as "credential accepted" ONLY when BOTH the HTTP
    status AND the documented error semantics match: a 400/404 carrying the
    documented UDAPI100010 (order not found) means auth passed and the probe
    id simply does not exist. Any other status/body is NOT classified as
    verified: 401/403 = rejected, 429 = rate limited, 5xx = provider error,
    unparseable/other = unexpected (fail closed).
    """
    status = response.status_code
    codes: set[str] = set()
    parse_ok = True
    try:
        payload = response.json()
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict):
                    codes.add(str(item.get("errorCode", "")))
    except ValueError:
        parse_ok = False
    if status in {401, 403}:
        return {"credential_accepted": False, "class": "auth_rejected",
                "http_status": status}
    if status == 429:
        return {"credential_accepted": False, "class": "rate_limited",
                "http_status": status, "codes": sorted(codes)}
    if status >= 500:
        return {"credential_accepted": False, "class": "provider_error",
                "http_status": status}
    if status in {400, 404} and "UDAPI100010" in codes:
        return {"credential_accepted": True, "class": "documented_not_found",
                "http_status": status, "codes": sorted(codes)}
    return {"credential_accepted": False, "class": "unexpected",
            "http_status": status, "codes": sorted(codes),
            "parse_ok": parse_ok}


class UpstoxSandboxBroker:
    """Real Upstox SANDBOX order flow with audited, idempotent reconciliation."""

    PLACE_PATH = "/v3/order/place"
    CANCEL_PATH = "/v3/order/cancel"
    MODIFY_PATH = "/v3/order/modify"

    #: Truthful identity of this broker. ``LIVE`` is False for sandbox and True
    #: for the real-money subclass in app.brokers.live; every label (repr,
    #: status_view, dashboard, reports) is derived from these attributes rather
    #: than hardcoded, so a real-money session can never be displayed as paper.
    MODE = "UPSTOX_SANDBOX"
    LIVE = False
    ALLOWED_HOSTS = SANDBOX_ALLOWED_HOSTS
    FORBIDDEN_HOSTS = SANDBOX_FORBIDDEN_HOSTS
    DEFAULT_BASE_URL = DEFAULT_SANDBOX_BASE_URL

    def __init__(
        self,
        access_token: str,
        instrument_key: str,
        *,
        base_url: str = DEFAULT_SANDBOX_BASE_URL,
        history_url: str = DEFAULT_ORDER_HISTORY_URL,
        product: str = "I",
        tag: str = "arena-v1",
        repository=None,
        initial_capital: float = 100_000.0,
        http_timeout: float = 20.0,
        transport=None,
        algo_name: str = "",
    ):
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("An access token is required")
        if base_url.split(":")[0] not in {"https", "http"}:
            raise ValueError("Base URL must be http(s)")
        host = host_of(base_url)
        if host in self.FORBIDDEN_HOSTS:
            # Non-overridable: a live/production host must never receive an
            # order from this class, whatever the configuration says.
            raise ValueError(
                f"Base URL host {host!r} is not permitted for "
                f"{self.MODE}: this class places orders and must only ever "
                f"point at {'sandbox' if not self.LIVE else 'live'} hosts "
                f"({', '.join(sorted(self.ALLOWED_HOSTS))})"
            )
        if host not in self.ALLOWED_HOSTS:
            # Previously this only logged SANDBOX_BASE_URL_REVIEW and continued,
            # which meant a typo in UPSTOX_SANDBOX_BASE_URL could aim a real
            # POST /v3/order/place at an arbitrary host. Refuse instead.
            raise ValueError(
                f"Base URL host {host!r} is not a recognised "
                f"{'live' if self.LIVE else 'sandbox'} host. "
                f"Permitted hosts: {', '.join(sorted(self.ALLOWED_HOSTS))}. "
                f"Set the base URL to one of them."
            )
        self._token = access_token
        self.instrument_key = instrument_key
        # 3.0.1: authoritative symbol → instrument_key resolution. The broker
        # NEVER silently substitutes its configured key for another symbol.
        primary_symbol = str(instrument_key).split("|")[-1]
        self.instrument_map: dict[str, str] = {primary_symbol: instrument_key}
        self._base_url = base_url.rstrip("/")
        self._history_url = history_url.rstrip("/")
        # Public read-only view used by verification drivers (never logged).
        self.history_endpoint = self._history_url
        self._product, self._tag = product, tag
        self.repository = repository
        self.ledger = SandboxLedger(initial_capital)
        self._timeout, self._transport = http_timeout, transport
        self._algo_name = algo_name
        self.available = True  # False after an auth rejection (fail closed)
        self.mode = self.MODE
        self.orders: dict[str, SandboxOrderState] = {}
        self.request_count = 0

    def __repr__(self):
        return (
            f"{type(self).__name__}(instrument_key={self.instrument_key!r}, "
            f"mode={self.mode!r}, live={self.LIVE})"
        )

    # -- audit routing -------------------------------------------------------
    #
    # Sandbox and live orders are recorded in SEPARATE tables so an operator
    # reading the database can never mistake a real fill for a simulated one.
    # The live subclass overrides these two hooks; every call site below goes
    # through them.

    def _record_order(self, record: dict) -> None:
        if self.repository is None:
            return
        self.repository.record_sandbox_order(record)

    def _record_transition(self, order_id: str, upstox_order_id: str | None,
                           status: str, filled_quantity: float | None,
                           average_price: float | None, ts: str, raw: dict) -> bool:
        if self.repository is None:
            return True
        return self.repository.record_sandbox_transition(
            order_id, upstox_order_id, status, filled_quantity, average_price, ts, raw)


    # -- HTTP core -----------------------------------------------------------

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._algo_name:
            headers["X-Algo-Name"] = self._algo_name  # optional per current docs
        return headers

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """HTTP core. 401/403 fail closed. Reads (GET) retry 429/5xx with
        bounded backoff; writes (POST/PUT/DELETE) NEVER auto-retry — the
        remote outcome could already exist (duplicate-order safety)."""
        if not self.available:
            raise SandboxAuthError("Sandbox broker disabled after an auth failure")
        import time as _time

        attempts = 2 if method.upper() == "GET" else 1
        for attempt in range(1, attempts + 1):
            self.request_count += 1
            try:
                if self._transport is not None:
                    with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                        response = client.request(method, url, headers=self._headers(), **kwargs)
                else:
                    response = httpx.request(
                        method, url, headers=self._headers(), timeout=self._timeout, **kwargs
                    )
            except httpx.HTTPError:
                if attempt < attempts:
                    _time.sleep(0.05)  # bounded, tests stay fast
                    continue
                raise SandboxRequestError(
                    f"{self.MODE} API unreachable (network error) at "
                    f"{host_of(url) or 'configured host'}")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts:
                    _time.sleep(0.05)
                    continue
            break
        if response.status_code in {401, 403}:
            self.available = False  # fail closed on auth errors
            logger.error("SANDBOX_AUTH_FAILURE", extra={"http_status": response.status_code})
            raise SandboxAuthError(
                f"Sandbox credential rejected (HTTP {response.status_code}); broker disabled"
            )
        return response

    # -- order flow ----------------------------------------------------------

    def resolve_instrument_key(self, symbol: str | None,
                               instrument_key: str | None = None) -> str:
        """Authoritative instrument identity for an order (3.0.1).

        - an explicit instrument_key wins and must agree with any mapping;
        - a mapped symbol resolves to exactly its key;
        - a symbol with NO mapping is REJECTED (3.0.2: the broker default key
          is NEVER used as an implicit fallback — missing identity is an
          error, not a guess);
        - any symbol/key mismatch or ambiguity is REJECTED, never defaulted.
        """
        if instrument_key:
            if symbol and symbol in self.instrument_map \
                    and self.instrument_map[symbol] != instrument_key:
                raise ValueError(
                    f"ambiguous instrument identity: symbol {symbol!r} maps to "
                    f"{self.instrument_map[symbol]!r}, not {instrument_key!r}")
            return instrument_key
        if symbol and symbol in self.instrument_map:
            return self.instrument_map[symbol]
        raise ValueError(
            f"missing instrument identity for symbol {symbol!r}: no mapping "
            f"and no explicit instrument_key; refusing to default "
            f"(3.0.2 fail-closed routing)")

    def submit(self, side: str, quantity: float, price: float, *,
               order_type: str = "MARKET", intent_id: str | None = None,
               symbol: str | None = None,
               instrument_key: str | None = None) -> str:
        """Place one sandbox order; returns OUR id (idempotent per intent_id).

        3.0.1 crash-safety: the intent is durably persisted (DURABLY_PERSISTED)
        BEFORE the remote call, marked SUBMISSION_ATTEMPTED before the POST and
        ACKNOWLEDGED / UNKNOWN after it. A 429/5xx (or network failure) marks
        UNKNOWN and NEVER retries the POST blindly — the order may exist
        remotely; only reconciliation may resolve it.
        """
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if float(quantity) != int(quantity):
            raise ValueError("quantity must be a whole share count")
        resolved_key = self.resolve_instrument_key(symbol, instrument_key)
        intent_id = intent_id or uuid4().hex
        # 3.0.2: durable, intent-derived broker tag (documented V3 `tag`
        # field, ≤20 chars, retrievable via the Order History API). It is
        # generated HERE and persisted BEFORE the POST so a lost response
        # can be resolved by remote lookup instead of a blind resubmission.
        intent_tag = intent_id[:12]
        existing = self.orders.get(intent_id)
        if existing is not None:
            return intent_id  # duplicate submission protection (in-process)
        if self.repository is not None:
            from app.engine.state import (SubmissionJournal, SUBMISSION_ATTEMPTED,
                                          ACKNOWLEDGED, UNKNOWN, FINAL_STATE,
                                          RECONCILED)

            self._journal = getattr(self, "_journal", None) or SubmissionJournal(
                self.repository)
            if self._journal.resubmission_forbidden(intent_id):
                # durable journal says this intent may already exist remotely
                logger.warning("DUPLICATE_INTENT_BLOCKED",
                               extra={"intent": intent_id[:8]})
                return intent_id
            self._journal.record(
                intent_id, instrument_key=resolved_key, symbol=symbol or "",
                side=side, quantity=float(quantity), price=float(price),
                strategy="", params_version="", tag=intent_tag)
            self._journal.mark(intent_id, SUBMISSION_ATTEMPTED)
        our_id = f"{'live' if self.LIVE else 'sbox'}-{intent_id[:12]}"
        state = SandboxOrderState(
            our_id=our_id, symbol=symbol or resolved_key.split("|")[-1],
            side=side, quantity=float(quantity), price=float(price),
        )
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": our_id, "intent_id": intent_id, "symbol": state.symbol,
            "side": side, "quantity": float(quantity), "price": float(price),
            "order_type": order_type, "status": "PENDING_SUBMIT",
            "tag": intent_tag, "created_ts": now, "updated_ts": now,
        }
        body = {
            "quantity": int(quantity),
            "product": self._product,
            "validity": "DAY",
            "price": 0 if order_type == "MARKET" else float(price),
            "tag": intent_tag,
            "instrument_token": resolved_key,
            "order_type": order_type,
            "transaction_type": side,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
            "slice": True,
            "market_protection": -1,
        }
        try:
            response = self._request("POST", f"{self._base_url}{self.PLACE_PATH}", json=body)
        except Exception:
            if self.repository is not None:
                # network failure: the POST may or may not have arrived
                self._journal.mark(intent_id, UNKNOWN,
                                   detail={"phase": "post_network_error"})
                if self.repository is not None:
                    record["status"] = "UNKNOWN"
                    self._record_order(record)
            raise
        if response.status_code in {429} or response.status_code >= 500:
            # Rate-limited / provider error: remote outcome is uncertain.
            if self.repository is not None:
                self._journal.mark(intent_id, UNKNOWN,
                                   detail={"http_status": response.status_code})
                record["status"] = "UNKNOWN"
                self._record_order(record)
            raise SandboxRequestError(
                f"Order outcome unknown (HTTP {response.status_code}) for {self.MODE}; "
                "intent kept UNKNOWN for reconciliation, never retried blindly")
        if response.status_code != 200:
            state.status = "REJECTED"
            record["status"] = "REJECTED"
            self.orders[intent_id] = state
            if self.repository:
                self._record_order(record)
                self._record_transition(
                    our_id, None, "REJECTED", None, None, now,
                    {"http_status": response.status_code},
                )
                self._journal.mark(intent_id, FINAL_STATE,
                                   detail={"outcome": "REJECTED",
                                           "http_status": response.status_code})
            raise SandboxRequestError(
                f"Order rejected with HTTP {response.status_code} ({self.MODE})"
            )
        try:
            upstox_ids = response.json()["data"]["order_ids"]
        except (KeyError, TypeError, ValueError):
            if self.repository is not None:
                self._journal.mark(intent_id, UNKNOWN,
                                   detail={"phase": "malformed_response"})
                record["status"] = "UNKNOWN"
                self._record_order(record)
            raise SandboxRequestError(
                f"Place response missing data.order_ids ({self.MODE})")
        if not isinstance(upstox_ids, list) or not upstox_ids:
            if self.repository is not None:
                self._journal.mark(intent_id, UNKNOWN,
                                   detail={"phase": "empty_order_ids"})
                record["status"] = "UNKNOWN"
                self._record_order(record)
            raise SandboxRequestError(
                f"Place response has no order ids ({self.MODE})")
        state.status = "SUBMITTED"
        state.upstox_order_ids = [str(item) for item in upstox_ids]
        record["upstox_order_id"] = state.upstox_order_ids[0]
        record["status"] = "SUBMITTED"
        record["upstox_order_ids"] = state.upstox_order_ids
        self.orders[intent_id] = state
        logger.info(
            "LIVE_ORDER_SUBMITTED" if self.LIVE else "SANDBOX_ORDER_SUBMITTED",
            extra={"side": side, "quantity": quantity, "order_type": order_type,
                   "instrument_key": resolved_key,
                   "slices": len(state.upstox_order_ids)},
        )
        if self.repository:
            self._record_order(record)
            self._record_transition(
                our_id, state.upstox_order_ids[0], "SUBMITTED", None, None, now,
                {"order_ids": state.upstox_order_ids},
            )
            self._journal.mark(intent_id, ACKNOWLEDGED,
                               upstox_order_ids=state.upstox_order_ids)
        return intent_id

    # -- UNKNOWN remote-order recovery (3.0.2) -------------------------------

    def lookup_intent_order(self, intent_id: str) -> str:
        """Resolve an UNKNOWN submission via the documented tag lookup.

        State machine: UNKNOWN → REMOTE_LOOKUP → VERIFIED_MATCH /
        VERIFIED_ABSENCE / UNRESOLVED (persisted; survives restart).

        - The intent's durable tag (persisted BEFORE the POST) is queried via
          the documented Order History API (GET history by ``tag``).
        - An order is ADOPTED only after strict attribute matching (tag,
          transaction_type, quantity, instrument_token, price for LIMIT).
        - Exactly one match → VERIFIED_MATCH (adopted; fills flow through the
          normal reconciliation). Zero matches on an authoritative response →
          VERIFIED_ABSENCE (final for THIS intent; never auto-resubmitted).
          Multiple matches → UNRESOLVED (fail closed). Unparseable/unavailable
          responses leave the intent UNKNOWN for a later lookup.
        - A blind re-POST is impossible from these states by construction.
        """
        from app.engine.state import (SubmissionJournal, UNKNOWN, REMOTE_LOOKUP,
                                      VERIFIED_MATCH, VERIFIED_ABSENCE,
                                      UNRESOLVED, ACKNOWLEDGED)

        journal = getattr(self, "_journal", None) or (
            SubmissionJournal(self.repository) if self.repository is not None
            else None)
        if journal is None:
            return "NO_JOURNAL"
        entry = journal.get(intent_id)
        if entry is None:
            return "UNKNOWN_INTENT"
        current = entry.get("state")
        if current not in {UNKNOWN, REMOTE_LOOKUP, UNRESOLVED}:
            return current  # nothing to look up
        tag = entry.get("tag") or intent_id[:12]
        journal.mark(intent_id, REMOTE_LOOKUP, detail={"tag": tag})
        try:
            response = self._request("GET", f"{self._history_url}",
                                     params={"tag": tag})
        except SandboxRequestError:
            journal.mark(intent_id, UNKNOWN, detail={"lookup": "network_error"})
            return UNKNOWN
        if response.status_code != 200:
            journal.mark(intent_id, UNKNOWN,
                         detail={"lookup": "http_status",
                                 "http_status": response.status_code})
            return UNKNOWN
        try:
            rows = response.json()["data"]
        except (KeyError, TypeError, ValueError):
            journal.mark(intent_id, UNKNOWN, detail={"lookup": "malformed"})
            return UNKNOWN
        if not isinstance(rows, list):
            journal.mark(intent_id, UNKNOWN, detail={"lookup": "malformed"})
            return UNKNOWN

        def _matches(row: dict) -> bool:
            if str(row.get("transaction_type", "")) != entry["side"]:
                return False
            try:
                # 3.0.3: a slice candidate carries its OWN child quantity —
                # any positive quantity up to the logical total is eligible;
                # candidate-level rules below enforce the exact total.
                row_quantity = int(row.get("quantity") or 0)
            except (TypeError, ValueError):
                return False
            if row_quantity <= 0 or row_quantity > int(entry["quantity"]):
                return False
            remote_key = row.get("instrument_token")
            if remote_key is not None and str(remote_key) != entry["instrument_key"]:
                return False
            if str(row.get("order_type", "MARKET")).upper() == "LIMIT":
                try:
                    if float(row.get("price") or 0) != float(entry["price"]):
                        return False
                except (TypeError, ValueError):
                    return False
            return True

        matched_orders: dict[str, list] = {}
        for row in rows:
            if not isinstance(row, dict) or not _matches(row):
                continue
            order_id = str(row.get("order_id") or "")
            if not order_id:
                continue
            matched_orders.setdefault(order_id, []).append(row)

        # 3.0.3 slice-aware adoption: the tag is intent-unique, so multiple
        # attribute-matching orders are the SLICED children of THIS intent
        # when their quantities sum exactly to the logical total. A single
        # candidate must carry the full quantity; anything else fails closed.
        def _latest_row(order_id: str) -> dict:
            return sorted(matched_orders[order_id],
                          key=lambda r: str(r.get("order_timestamp") or ""))[-1]

        candidate_ids = sorted(matched_orders)
        total_quantity = float(entry["quantity"])
        adopted_ids: list[str] = []
        if len(candidate_ids) == 1:
            only = candidate_ids[0]
            if float(_latest_row(only).get("quantity") or 0) == total_quantity:
                adopted_ids = [only]
        elif candidate_ids:
            slice_sum = sum(float(_latest_row(oid).get("quantity") or 0)
                            for oid in candidate_ids)
            if abs(slice_sum - total_quantity) < 1e-9:
                adopted_ids = candidate_ids
        if candidate_ids and not adopted_ids:
            if len(candidate_ids) == 1:
                # authoritative lookup, but the only candidate does not match
                # THIS intent's full attributes → nothing of ours exists
                journal.mark(intent_id, VERIFIED_ABSENCE,
                             detail={"resolution": "attribute_mismatch"})
                logger.warning("REMOTE_LOOKUP_ABSENT",
                               extra={"intent": intent_id[:8]})
                return VERIFIED_ABSENCE
            journal.mark(intent_id, UNRESOLVED,
                         detail={"matches": sorted(matched_orders),
                                 "resolution": "fail_closed"})
            logger.warning("REMOTE_LOOKUP_AMBIGUOUS",
                           extra={"intent": intent_id[:8],
                                  "matches": len(matched_orders)})
            return UNRESOLVED
        if not adopted_ids:
            journal.mark(intent_id, VERIFIED_ABSENCE,
                         detail={"resolution": "authoritative_empty_lookup"})
            logger.warning("REMOTE_LOOKUP_ABSENT",
                           extra={"intent": intent_id[:8]})
            return VERIFIED_ABSENCE
        state = self.orders.get(intent_id)
        if state is None:
            state = SandboxOrderState(
                our_id=f"{'live' if self.LIVE else 'sbox'}-{intent_id[:12]}",
                symbol=entry.get("symbol", ""),
                side=entry["side"], quantity=float(entry["quantity"]),
                price=float(entry["price"]))
        # adopt EVERY child; per-child remote statuses are audited, fills stay
        # unapplied (ledger applied nothing yet) so the reconciliation loop
        # right after adoption applies each child's delta exactly once
        state.upstox_order_ids = list(adopted_ids)
        state.child_status = {}
        remote_statuses: dict[str, str] = {}
        for order_id in adopted_ids:
            final = _latest_row(order_id)
            mapped = self._map_status(str(final.get("status", "")),
                                      float(final.get("filled_quantity") or 0))
            remote_statuses[order_id] = mapped
            state.child_status[order_id] = mapped
            if self.repository is not None:
                self._record_transition(
                    state.our_id, order_id, "REMOTE_LOOKUP_ADOPTED", None, None,
                    str(final.get("order_timestamp") or ""),
                    {"adopted_via": "REMOTE_LOOKUP", "tag": tag,
                     "remote_status": mapped})
        # NOTE: filled_quantity stays 0 — the LEDGER has not applied anything
        # yet; the normal reconciliation loop applies each child's full delta
        # exactly once right after adoption. Status stays NON-final even when
        # every remote child is already final, so those fills are applied.
        state.status = "SUBMITTED"
        self.orders[intent_id] = state
        journal.mark(intent_id, VERIFIED_MATCH,
                     detail={"order_ids": adopted_ids,
                             "status": remote_statuses})
        journal.mark(intent_id, ACKNOWLEDGED,
                     upstox_order_ids=list(adopted_ids))
        logger.warning("REMOTE_LOOKUP_ADOPTED",
                       extra={"intent": intent_id[:8],
                              "order_ids": len(adopted_ids)})
        return VERIFIED_MATCH

    def reconcile(self) -> list[dict]:
        """Pull documented order history for submitted orders; apply fills once.

        3.0.2: UNKNOWN submissions are resolved FIRST via the documented
        tag lookup (never re-POSTed). 3.0.3: EVERY child (slice) order id is
        reconciled independently — each child's cumulative filled quantity is
        compared to ITS OWN baseline, the delta is applied at the INCREMENTAL
        fill basis (never the cumulative average again), and the logical
        filled quantity is the sum over children. Duplicate history rows are
        recorded once (idempotent transitions keyed by child id), so restarts
        never double count and no child is ever lost or applied twice.
        """
        new_fills: list[dict] = []
        if self.repository is not None:
            from app.engine.state import (SubmissionJournal, UNKNOWN,
                                          REMOTE_LOOKUP, UNRESOLVED)
            journal = getattr(self, "_journal", None) or SubmissionJournal(
                self.repository)
            for intent_id, entry in journal.unresolved().items():
                if entry.get("state") in {UNKNOWN, REMOTE_LOOKUP, UNRESOLVED}:
                    self.lookup_intent_order(intent_id)
        for intent_id, state in list(self.orders.items()):
            if state.status in {"FILLED", "CANCELLED", "REJECTED"} or not state.upstox_order_ids:
                continue
            for upstox_order_id in state.upstox_order_ids:
                response = self._request(
                    "GET",
                    f"{self._history_url}",
                    params={"order_id": upstox_order_id},
                )
                if response.status_code != 200:
                    logger.warning(
                        "SANDBOX_RECONCILE_ERROR",
                        extra={"http_status": response.status_code},
                    )
                    continue
                try:
                    history = response.json()["data"]
                except (KeyError, TypeError, ValueError):
                    raise SandboxRequestError(
                    f"Order-history response missing data ({self.MODE})")
                final = history[-1] if isinstance(history, list) and history else None
                if final is None:
                    continue
                status = str(final.get("status", ""))
                filled = float(final.get("filled_quantity") or 0)
                average = float(final.get("average_price") or 0)
                ts = str(final.get("order_timestamp") or "")
                mapped = self._map_status(status, filled)
                if self.repository:
                    fresh = self._record_transition(
                        state.our_id, upstox_order_id, mapped, filled, average, ts, final
                    )
                else:
                    fresh = True  # without a repository, apply conservatively
                if not fresh:
                    continue  # already applied (idempotent across restarts)
                # 3.0.3 per-child delta fill application: the delta is THIS
                # child's cumulative growth, priced at the incremental basis
                # (f_now·avg_now − f_prev·avg_prev)/Δ — a partial-then-cancelled
                # child still credits exactly its executed quantity, exactly
                # once, and sibling children can never swallow each other's
                # fills.
                prev_filled = float(state.child_filled.get(upstox_order_id, 0.0))
                prev_avg = float(state.child_avg_price.get(upstox_order_id, 0.0))
                if filled > prev_filled:
                    delta = filled - prev_filled
                    incremental = average
                    if prev_filled > 0 and average > 0:
                        incremental = (filled * average - prev_filled * prev_avg) / delta
                        if incremental < 0:  # anomalous remote basis; never negative
                            incremental = average
                    elif average <= 0 and prev_avg > 0:
                        incremental = prev_avg
                    self.ledger.apply_fill(state.side, delta, incremental)
                    state.child_filled[upstox_order_id] = filled
                    state.child_avg_price[upstox_order_id] = \
                        average if average > 0 else prev_avg
                    state.filled_quantity = sum(state.child_filled.values())
                    new_fills.append(
                        {"intent_id": intent_id, "order_id": state.our_id,
                         "upstox_order_id": upstox_order_id,
                         "side": state.side, "quantity": delta,
                         "price": incremental}
                    )
                    logger.info(
                        "SANDBOX_FILL_RECONCILED",
                        extra={"side": state.side, "quantity": delta,
                               "price": incremental, "child": upstox_order_id},
                    )
                state.child_status[upstox_order_id] = mapped
            # honest aggregate status across ALL children (never one child's
            # status alone deciding the logical order)
            state.status = self._aggregate_child_status(state)
            if self.repository is not None:
                from app.engine.state import SubmissionJournal, FINAL_STATE, RECONCILED

                journal = getattr(self, "_journal", None) or SubmissionJournal(
                    self.repository)
                if state.status in {"FILLED", "CANCELLED", "REJECTED"}:
                    journal.mark(intent_id, FINAL_STATE,
                                 detail={"outcome": state.status})
                elif state.filled_quantity > 0:
                    journal.mark(intent_id, RECONCILED,
                                 detail={"filled": state.filled_quantity})
        return new_fills

    @staticmethod
    def _map_status(provider_status: str, filled_quantity: float) -> str:
        lowered = provider_status.lower()
        if lowered in {"complete", "completed"}:
            return "FILLED"
        if "reject" in lowered:
            return "REJECTED"
        if "cancel" in lowered:
            return "CANCELLED"
        if lowered in {"open", "open pending", "put order req received",
                       "validation pending", "trigger pending"}:
            return "PARTIALLY_FILLED" if filled_quantity > 0 else "OPEN"
        return "OPEN"

    # -- cancellation (3.0.2: never lie about a cancellation) ----------------

    def _child_remote_status(self, upstox_order_id: str) -> str | None:
        """Best-effort documented order-history lookup for ONE child order.

        Returns the mapped status (FILLED/CANCELLED/REJECTED/OPEN/...) or
        None when the remote state cannot be determined (never guessed).
        """
        try:
            response = self._request(
                "GET", f"{self._history_url}",
                params={"order_id": upstox_order_id})
        except SandboxRequestError:
            return None
        if response.status_code != 200:
            return None
        try:
            history = response.json()["data"]
        except (KeyError, TypeError, ValueError):
            return None
        final = history[-1] if isinstance(history, list) and history else None
        if final is None:
            return None
        return self._map_status(str(final.get("status", "")),
                                float(final.get("filled_quantity") or 0))

    def _aggregate_child_status(self, state: SandboxOrderState) -> str:
        """Honest logical status from per-child remote statuses."""
        statuses = [state.child_status.get(oid) for oid in state.upstox_order_ids]
        if not statuses:
            return state.status
        if all(s == "CANCELLED" for s in statuses):
            return "CANCELLED"
        if any(s == "FILLED" for s in statuses) and \
                all(s in {"FILLED", "CANCELLED", "REJECTED"} for s in statuses):
            return "FILLED"
        if any(s in {"OPEN", "SUBMITTED", "PENDING_SUBMIT", "PARTIALLY_FILLED"}
               for s in statuses):
            return "PARTIALLY_FILLED" if state.filled_quantity > 0 else "SUBMITTED"
        return state.status

    def cancel(self, intent_id: str) -> dict:
        """Cancel every child order of a logical order; only report CANCELLED
        when the broker CONFIRMED each child (3.0.2).

        - DELETE 200            → child CONFIRMED cancelled.
        - DELETE rejected (4xx) → outcome NOT assumed; the child is looked up
          via documented order history (it may already be filled/cancelled
          or still working).
        - DELETE network error / 5xx → outcome UNKNOWN; the child remains
          working until reconciliation proves otherwise.
        The logical order is CANCELLED only when ALL children are confirmed
        cancelled; a still-working remote order is never hidden.
        """
        state = self.orders.get(intent_id)
        if state is None:
            raise ValueError("Unknown sandbox order intent")
        if state.status in {"FILLED", "CANCELLED", "REJECTED"}:
            return {"status": state.status, "already_final": True}
        if not state.upstox_order_ids:
            return {"status": state.status, "already_final": True,
                    "note": "never submitted remotely"}
        results = []
        for upstox_order_id in state.upstox_order_ids:
            if state.child_status.get(upstox_order_id) == "CANCELLED":
                results.append({"order_id": upstox_order_id, "cancelled": True,
                                "confirmed": True})
                continue
            try:
                response = self._request(
                    "DELETE",
                    f"{self._base_url}{self.CANCEL_PATH}",
                    params={"order_id": upstox_order_id},
                )
            except Exception as exc:  # network failure: outcome unknown
                state.child_status[upstox_order_id] = "CANCEL_UNKNOWN"
                results.append({"order_id": upstox_order_id, "cancelled": False,
                                "confirmed": False, "error": type(exc).__name__})
                logger.warning("SANDBOX_CANCEL_OUTCOME_UNKNOWN",
                               extra={"order_id": upstox_order_id,
                                      "error": type(exc).__name__})
                continue
            if response.status_code == 200:
                state.child_status[upstox_order_id] = "CANCELLED"
                results.append({"order_id": upstox_order_id, "cancelled": True,
                                "confirmed": True})
            else:
                # Documented: cancelling a finalized order is rejected — but
                # we do NOT guess which case this is; reconcile the child.
                state.child_status[upstox_order_id] = "CANCEL_UNKNOWN"
                results.append({"order_id": upstox_order_id, "cancelled": False,
                                "confirmed": False,
                                "http_status": response.status_code})
        # resolve every unconfirmed child through the documented history API
        for upstox_order_id in state.upstox_order_ids:
            if state.child_status.get(upstox_order_id) != "CANCEL_UNKNOWN":
                continue
            remote = self._child_remote_status(upstox_order_id)
            if remote is not None:
                state.child_status[upstox_order_id] = remote
            else:
                # still unknown → the child REMAINS working; never assume
                state.child_status[upstox_order_id] = "SUBMITTED"
        logical = self._aggregate_child_status(state)
        state.status = logical
        if self.repository:
            self._record_transition(
                state.our_id,
                state.upstox_order_ids[0] if state.upstox_order_ids else None,
                logical, None, None, datetime.now(timezone.utc).isoformat(),
                {"results": results,
                 "child_status": dict(state.child_status)},
            )
            if logical == "CANCELLED":
                from app.engine.state import FINAL_STATE

                journal = getattr(self, "_journal", None)
                if journal is not None:
                    journal.mark(intent_id, FINAL_STATE,
                                 detail={"outcome": "CANCELLED"})
        if logical == "CANCELLED":
            logger.info("SANDBOX_ORDER_CANCELLED",
                        extra={"orders": len(results)})
        else:
            logger.warning("SANDBOX_CANCEL_NOT_CONFIRMED",
                           extra={"intent": intent_id[:8], "status": logical})
        return {"status": logical, "results": results}

    def modify(self, intent_id: str, *, quantity: float | None = None,
               price: float | None = None, order_type: str | None = None) -> dict:
        """Documented V3 modify (supported flow; engine uses it sparingly)."""
        state = self.orders.get(intent_id)
        if state is None or not state.upstox_order_ids:
            raise ValueError("Unknown or unsubmitted sandbox order intent")
        body = {
            "quantity": int(quantity if quantity is not None else state.quantity),
            "validity": "DAY",
            "price": float(price if price is not None else (state.price or 0)),
            "order_id": state.upstox_order_ids[0],
            "order_type": order_type or "MARKET",
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "market_protection": -1,
        }
        response = self._request(
            "PUT", f"{self._base_url}{self.MODIFY_PATH}", json=body
        )
        if response.status_code != 200:
            raise SandboxRequestError(f"Sandbox modify failed with HTTP {response.status_code}")
        state.quantity = float(body["quantity"])
        state.price = float(body["price"])
        return {"status": "MODIFIED", "order_id": state.upstox_order_ids[0]}

    def open_orders(self) -> list[SandboxOrderState]:
        return [
            state for state in self.orders.values()
            if state.status in {"PENDING_SUBMIT", "SUBMITTED", "OPEN", "PARTIALLY_FILLED"}
        ]

    def status_view(self) -> dict:
        return {
            "mode": self.mode,
            "live": self.LIVE,
            "available": self.available,
            "instrument_key": self.instrument_key,
            "orders_tracked": len(self.orders),
            "open_orders": len(self.open_orders()),
            "ledger": self.ledger.snapshot(),
            "request_count": self.request_count,
            "base_url": self._base_url,
        }
