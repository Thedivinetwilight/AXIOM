"""The single authoritative live execution gateway (FINAL-11/10 §3–§13).

Within AXIOM's first-party live execution code, one source of truth decides
whether a REAL-MONEY order may leave the building. The CLI, GUI, research,
recovery and background jobs must arrive here or do not submit. This in-process
application boundary does not control arbitrary HTTP requests made outside
AXIOM by another process or by code that already holds broker credentials.

Pipeline for every live submission:

    SIGNAL / PURPOSE
    -> EXECUTION INTENT (complete identity, immutable)
    -> AUTHORIZATION EPOCH (current, valid, matches this activation)
    -> SESSION STATE (ACTIVE for entries; exits may reduce risk)
    -> EMERGENCY-STOP SEMANTICS (entries blocked, risk-reduction allowed)
    -> EXACT CONFIRMATION PHRASE (gateway layer, independent)
    -> BROKER IDENTITY (a live broker, correct account scope)
    -> GUARD CHAIN (the existing chain, invoked for real here)
    -> DUPLICATE CHECK (recent intent ids + signal identity)
    -> DURABLE AUDIT (written BEFORE the remote call)
    -> BROKER SUBMISSION
    -> outcome recording (UNKNOWN when the response is lost)

A refusal at any layer guarantees BROKER SUBMIT = 0. The gateway counts
every submission attempt so adversarial tests can assert that a failing
guard never reaches the broker.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import threading
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import WeakKeyDictionary

from app.config.settings import LIVE_CONFIRM_PHRASE

_GATEWAY_CAPABILITY_LOCK = threading.RLock()
_GATEWAY_CAPABILITIES = WeakKeyDictionary()
_ACTIVE_BROKER_DISPATCH = ContextVar("axiom_active_broker_dispatch", default=None)


def _register_gateway_capability(gateway):
    """Mint and register one opaque capability per gateway, exactly once.

    Callers cannot supply or replace the credential. The constructor keeps the
    returned object local while broker binding is established; later attempts
    to re-register the gateway fail closed instead of replacing the binding.
    """
    with _GATEWAY_CAPABILITY_LOCK:
        if gateway in _GATEWAY_CAPABILITIES:
            raise RuntimeError("gateway capability is already registered")
        capability = object()
        _GATEWAY_CAPABILITIES[gateway] = capability
        return capability


def _gateway_capability(gateway):
    with _GATEWAY_CAPABILITY_LOCK:
        return _GATEWAY_CAPABILITIES.get(gateway)


PURPOSES = ("ENTRY", "EXIT", "RISK_REDUCE", "CANCEL", "RECONCILE")

#: purposes allowed while the emergency stop is active — they never add risk
EMERGENCY_SAFE_PURPOSES = frozenset({"EXIT", "RISK_REDUCE", "CANCEL", "RECONCILE"})

#: purposes that require the session to be fully ACTIVE
ACTIVE_REQUIRED_PURPOSES = frozenset({"ENTRY"})

#: purposes allowed from ACTIVE, PAUSED or RECONCILING (risk management)
RISK_MANAGEMENT_PURPOSES = frozenset({"EXIT", "RISK_REDUCE", "CANCEL", "RECONCILE"})

EPOCH_KEY = "live_authorization_epoch"


class GatewayRefusal(RuntimeError):
    """A submission was refused BEFORE any broker call. Carries the layer."""

    def __init__(self, layer: str, reason: str):
        super().__init__(f"{layer}: {reason}")
        self.layer = layer
        self.reason = reason


# =====================================================================
# EXECUTION INTENT (FINAL-11/10 §5)
# =====================================================================

@dataclass(frozen=True)
class ExecutionIntent:
    """Immutable identity of one live order decision.

    Carries enough context to reconstruct WHY the order exists. Any missing
    required field is a structural refusal — a partial intent cannot be
    authorized.
    """

    purpose: str
    market_id: str
    identity: str
    side: str
    quantity: float
    order_type: str = "MARKET"
    price: float | None = None
    trigger_price: float | None = None
    account_scope: str = ""
    universe: str = ""
    strategy_id: str = ""
    strategy_lineage: str = ""
    signal_id: str = ""
    signal_ts: float | None = None
    evidence_snapshot_id: str = ""
    risk_snapshot_id: str = ""
    configuration_hash: str = ""
    model_revision: str = ""
    authorization_epoch: int | None = None
    build_identity: str = ""
    cancel_order_id: str | None = None
    intent_id: str = field(default_factory=lambda: uuid4().hex)
    created_ts: float = field(default_factory=time.time)

    REQUIRED_FOR_ALL = ("purpose", "market_id", "identity", "side", "quantity")
    REQUIRED_FOR_ENTRY = ("strategy_id", "signal_id", "signal_ts",
                          "configuration_hash", "account_scope")

    def validate(self) -> list[str]:
        problems = []
        if self.purpose not in PURPOSES:
            problems.append(f"purpose must be one of {PURPOSES}")
        if not isinstance(self.market_id, str) or not self.market_id.strip():
            problems.append("market_id is required")
        if not isinstance(self.identity, str) or not self.identity.strip():
            problems.append("identity is required")
        if self.side not in ("BUY", "SELL"):
            problems.append("side must be BUY or SELL")
        if (isinstance(self.quantity, bool)
                or not isinstance(self.quantity, (int, float))
                or not math.isfinite(float(self.quantity))
                or self.quantity <= 0):
            problems.append("quantity must be a positive finite number")
        if not isinstance(self.order_type, str) or self.order_type.upper() not in {
                "MARKET", "LIMIT", "SL", "SL-M"}:
            problems.append("order_type must be MARKET, LIMIT, SL, or SL-M")
        if self.price is not None:
            if (isinstance(self.price, bool)
                    or not isinstance(self.price, (int, float))
                    or not math.isfinite(float(self.price)) or self.price < 0):
                problems.append("price must be a finite non-negative number")
            elif isinstance(self.order_type, str) and self.order_type.upper() != "MARKET" \
                    and self.price <= 0:
                problems.append("non-market order price must be positive")
        elif isinstance(self.order_type, str) and self.order_type.upper() != "MARKET":
            problems.append("non-market order requires a price")
        if self.trigger_price is not None and (
                isinstance(self.trigger_price, bool)
                or not isinstance(self.trigger_price, (int, float))
                or not math.isfinite(float(self.trigger_price))
                or self.trigger_price <= 0):
            problems.append("trigger_price must be a positive finite number")
        if self.signal_ts is not None and (
                isinstance(self.signal_ts, bool)
                or not isinstance(self.signal_ts, (int, float))
                or not math.isfinite(float(self.signal_ts))
                or self.signal_ts <= 0):
            problems.append("signal_ts must be a positive finite timestamp")
        if (isinstance(self.created_ts, bool)
                or not isinstance(self.created_ts, (int, float))
                or not math.isfinite(float(self.created_ts))
                or self.created_ts <= 0):
            problems.append("created_ts must be a positive finite timestamp")
        if not isinstance(self.intent_id, str) or not self.intent_id.strip():
            problems.append("intent_id is required")

        if self.purpose == "ENTRY":
            for name in self.REQUIRED_FOR_ENTRY:
                value = getattr(self, name)
                if name == "signal_ts":
                    if (isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(float(value)) or value <= 0):
                        problems.append("ENTRY intent requires signal_ts")
                elif not isinstance(value, str) or not value.strip():
                    problems.append(f"ENTRY intent requires {name}")
            if (isinstance(self.authorization_epoch, bool)
                    or not isinstance(self.authorization_epoch, int)
                    or self.authorization_epoch <= 0):
                problems.append("ENTRY intent requires a positive integer authorization epoch")
            if self.side != "BUY":
                problems.append("long-only ENTRY intent must use BUY side")
            if (isinstance(self.price, bool)
                    or not isinstance(self.price, (int, float))
                    or not math.isfinite(float(self.price)) or self.price <= 0):
                problems.append("ENTRY intent requires a positive finite reference price")
        if self.purpose in {"EXIT", "RISK_REDUCE"} and self.side != "SELL":
            problems.append("long-only risk-reduction intent must use SELL side")
        if self.purpose == "CANCEL" and not isinstance(self.cancel_order_id, str) or self.purpose == "CANCEL" and not self.cancel_order_id.strip():
            problems.append("CANCEL intent requires the target order id")
        return problems

    def fingerprint(self) -> str:
        """Deterministic identity for duplicate detection."""
        payload = json.dumps(
            [self.purpose, self.market_id, self.identity, self.side,
             float(self.quantity), self.order_type, self.signal_id or "",
             self.cancel_order_id or ""],
            sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


# =====================================================================
# AUTHORIZATION EPOCH (FINAL-11/10 §6)
# =====================================================================

class AuthorizationEpochs:
    """Persistent live authorization epochs.

    Activation mints a fresh epoch; stop/disarm/block/emergency invalidate
    it. A stale engine carrying an old epoch cannot submit: the gateway
    compares against the stored, durable epoch — never against in-process
    memory alone.
    """

    def __init__(self, repository, *, clock=time.time):
        self.repository = repository
        self.clock = clock

    def _load(self) -> dict | None:
        if self.repository is None:
            return None
        state = self.repository.get_engine_state(EPOCH_KEY, None)
        if state is None:
            return None
        if not isinstance(state, dict):
            raise ValueError("authorization epoch record is malformed")
        return state

    def current(self) -> dict | None:
        return self._load()

    def activate(self, *, actor: str = "gui", session_state: str = "ACTIVE",
                 configuration_hash: str | None = None) -> dict:
        """Atomically mint a fresh epoch bound to verified risk settings."""
        import json

        database = getattr(self.repository, "db", None)
        if database is None:
            raise RuntimeError("atomic authorization epoch storage is unavailable")
        with database.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM engine_state WHERE key=?",
                             (EPOCH_KEY,)).fetchone()
            previous = None
            if row is not None:
                try:
                    previous = json.loads(row[0])
                except (TypeError, ValueError) as exc:
                    raise ValueError("authorization epoch record is unreadable") from exc
                if (not isinstance(previous, dict)
                        or type(previous.get("epoch")) is not int
                        or previous.get("epoch", 0) <= 0
                        or type(previous.get("valid")) is not bool):
                    raise ValueError("authorization epoch record is malformed")
            prior_epoch = previous.get("epoch", 0) if previous else 0
            if prior_epoch >= 2**63 - 1:
                raise ValueError("authorization epoch counter is exhausted")
            prior_superseded = (previous.get("superseded_epochs", 0)
                                if previous else 0)
            if type(prior_superseded) is not int or prior_superseded < 0:
                raise ValueError("authorization epoch history is malformed")
            was_valid = previous.get("valid", False) if previous else False
            state = {
                "epoch": prior_epoch + 1,
                "activated_ts": self.clock(),
                "actor": str(actor),
                "session_state": session_state,
                "configuration_hash": str(configuration_hash or ""),
                "valid": True,
                "superseded_epochs": prior_superseded + (1 if was_valid else 0),
            }
            payload = json.dumps(state, allow_nan=False, separators=(",", ":"))
            db.execute(
                "INSERT INTO engine_state(key,payload) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (EPOCH_KEY, payload))
        return state

    def invalidate(self, *, reason: str) -> dict | None:
        import json

        database = getattr(self.repository, "db", None)
        if database is None:
            raise RuntimeError("atomic authorization epoch storage is unavailable")
        with database.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM engine_state WHERE key=?",
                             (EPOCH_KEY,)).fetchone()
            if row is None:
                return None
            try:
                state = json.loads(row[0])
            except (TypeError, ValueError) as exc:
                raise ValueError("authorization epoch record is unreadable") from exc
            if (not isinstance(state, dict)
                    or type(state.get("epoch")) is not int
                    or state.get("epoch", 0) <= 0
                    or type(state.get("valid")) is not bool):
                raise ValueError("authorization epoch record is malformed")
            state = {**state, "valid": False,
                     "invalidated_ts": self.clock(),
                     "invalidation_reason": str(reason)}
            payload = json.dumps(state, allow_nan=False, separators=(",", ":"))
            db.execute("UPDATE engine_state SET payload=? WHERE key=?",
                       (payload, EPOCH_KEY))
        return state

    def validate(self, epoch: int | None, *,
                 configuration_hash: str | None = None) -> tuple[bool, str]:
        """True only for the CURRENT epoch while it remains valid."""
        try:
            state = self._load()
        except Exception as exc:
            return False, f"authorization epoch could not be read ({type(exc).__name__})"
        if state is None:
            return False, "no authorization epoch exists (live never activated here)"
        current = state.get("epoch")
        if (type(current) is not int or current <= 0
                or type(state.get("valid")) is not bool):
            return False, "authorization epoch record is malformed"
        if state.get("valid") is not True:
            return False, ("authorization epoch was invalidated "
                           f"({state.get('invalidation_reason', 'no reason recorded')})")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            return False, "intent carries no valid positive authorization epoch"
        if epoch != current:
            return False, (f"stale authorization epoch {epoch}; current is {current}")
        if configuration_hash is not None:
            stored_hash = state.get("configuration_hash")
            if (not isinstance(stored_hash, str) or not stored_hash
                    or stored_hash != configuration_hash):
                return False, "authorization epoch does not match the current configuration"
        return True, "epoch current and valid"


# =====================================================================
# THE GATEWAY
# =====================================================================

class LiveExecutionGateway:
    """The ONLY path to a live broker (FINAL-11/10 §4)."""

    def __init__(self, *, settings, repository, session_manager, broker,
                 guard_chain=None, epochs: AuthorizationEpochs | None = None,
                 emergency_stop_reader=None, account_scope: str = "",
                 context_provider=None, lease_owner: str | None = None,
                 live_ops=None, clock=time.time, memory: int = 256):
        self.settings = settings
        self.repository = repository
        self.sessions = session_manager
        self.broker = broker
        # The concrete LIVE client also enforces that mutating HTTP operations
        # occur inside a gateway-authorized call. Test doubles may omit this
        # private binder; the production Upstox client cannot.
        broker_capability = _register_gateway_capability(self)
        # Bind through the internal production composition function, not a
        # mutable method on the broker that any caller could invoke with its
        # own token. Protocol doubles deliberately remain unbound.
        from app.brokers.live import _bind_live_gateway

        self._broker_capability_bound = _bind_live_gateway(
            self, broker, broker_capability)
        self.guard_chain = guard_chain
        self.epochs = epochs or AuthorizationEpochs(repository, clock=clock)
        if emergency_stop_reader is None:
            from app.live.ops import read_emergency_stop

            emergency_stop_reader = read_emergency_stop
        self.emergency_stop_reader = emergency_stop_reader
        self.account_scope = str(account_scope or
                                 getattr(broker, "account_scope", "") or "")
        self.live_ops = live_ops
        if self.live_ops is None:
            try:
                from app.live.ops import LiveOperations

                self.live_ops = LiveOperations.from_settings(
                    settings, repository, broker,
                    now_fn=lambda: datetime.fromtimestamp(
                        float(clock()), UTC))
            except Exception:
                # Entries below require these durable operational guards;
                # inability to construct them never degrades to allow.
                self.live_ops = None
        from app.live.lock import owner_id

        # A real-money submission must be made by this process instance, not
        # merely by another process that later reused its numeric PID.
        self.lease_owner = str(lease_owner or owner_id())
        #: callable() -> dict of live context (exposure, breaches, canary);
        #: failures are treated as UNKNOWN and fail closed in the chain.
        self.context_provider = context_provider
        self.clock = clock
        from collections import deque
        self._recent_fingerprints = deque(maxlen=max(16, int(memory)))
        self.submission_attempts = 0     # every call that reached this method
        self.broker_calls = 0            # every call that reached the broker
        self.refusals: list[dict] = []

    # ------------------------------------------------------------- refuse --
    def _refuse(self, layer: str, reason: str, intent: ExecutionIntent | None) -> dict:
        record = {"authorized": False, "layer": layer, "reason": reason,
                  "intent_id": intent.intent_id if intent else None,
                  "ts": self.clock()}
        self.refusals.append(record)
        if len(self.refusals) > 200:
            del self.refusals[: len(self.refusals) - 200]
        self._audit("LIVE_INTENT_REFUSED", record)
        raise GatewayRefusal(layer, reason)

    def _audit(self, kind: str, payload: dict) -> None:
        """Durable audit; an audit failure must never masquerade as an
        authorization — it fails the submission instead (fail closed)."""
        if self.repository is None:
            return
        try:
            self.repository.record_system_event(kind, payload)
        except Exception as exc:
            raise GatewayRefusal("DURABLE AUDIT",
                                 f"audit could not be written ({type(exc).__name__}); "
                                 "refusing rather than executing unrecorded")

    @staticmethod
    def _whole_quantity(value, *, field_name: str, allow_negative: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{field_name} is not numeric")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field_name} is not numeric") from exc
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"{field_name} is not a finite whole quantity")
        if not allow_negative and number < 0:
            raise ValueError(f"{field_name} is negative")
        return int(number)

    def _authorize_position_reduction(self, intent: ExecutionIntent) -> dict | None:
        """Prove a long-only SELL reduces a live position by no more than the
        currently available quantity, including pending SELL orders.

        The broker snapshot is re-read at the final authorization boundary.
        Unknown broker capability, stale/malformed response, or a missing
        position refuses the order; the local strategy ledger is not treated
        as authority for the account's actual position.
        """
        if intent.purpose not in {"EXIT", "RISK_REDUCE"}:
            return None
        if intent.side != "SELL":
            self._refuse("INTENT", "long-only risk reduction must use SELL side", intent)

        positions_reader = getattr(self.broker, "get_positions", None)
        orders_reader = getattr(self.broker, "get_open_orders", None)
        product = getattr(self.broker, "_product", None)
        if not callable(positions_reader) or not callable(orders_reader) \
                or not isinstance(product, str) or not product.strip():
            self._refuse(
                "POSITION REDUCTION",
                "broker does not expose authoritative positions and open orders; "
                "refusing to assume a holding",
                intent)
        try:
            positions = positions_reader()
            orders = orders_reader()
        except Exception as exc:
            self._refuse(
                "POSITION REDUCTION",
                f"broker position/order snapshot unavailable ({type(exc).__name__})",
                intent)
        if not isinstance(positions, list) or not isinstance(orders, list):
            self._refuse("POSITION REDUCTION",
                         "broker position/order snapshot has an invalid shape", intent)

        net_quantity = 0
        try:
            for row in positions:
                if not isinstance(row, dict):
                    raise ValueError("position row is malformed")
                instrument = row.get("instrument_token")
                row_product = row.get("product")
                if (not isinstance(instrument, str) or not instrument.strip()
                        or not isinstance(row_product, str) or not row_product.strip()):
                    raise ValueError("position identity is malformed")
                quantity = self._whole_quantity(
                    row.get("quantity"), field_name="position quantity",
                    allow_negative=True)
                if instrument == intent.identity and row_product == product:
                    net_quantity += quantity

            reserved_sells = 0
            terminal = {"complete", "completed", "rejected", "cancelled", "canceled"}
            for row in orders:
                if not isinstance(row, dict):
                    raise ValueError("order row is malformed")
                instrument = row.get("instrument_token")
                row_product = row.get("product")
                side = row.get("transaction_type")
                status = row.get("status")
                if (not isinstance(instrument, str) or not instrument.strip()
                        or not isinstance(row_product, str) or not row_product.strip()
                        or side not in {"BUY", "SELL"}
                        or not isinstance(status, str) or not status.strip()):
                    raise ValueError("order identity/status is malformed")
                if (instrument != intent.identity or row_product != product
                        or side != "SELL"):
                    continue
                if row.get("pending_quantity") is None and status.lower() not in terminal:
                    raise ValueError("working SELL order has unknown remaining quantity")
                pending = (0 if status.lower() in terminal and
                           row.get("pending_quantity") is None else
                           self._whole_quantity(row.get("pending_quantity"),
                                                field_name="pending SELL quantity"))
                reserved_sells += pending
        except (TypeError, ValueError, OverflowError) as exc:
            self._refuse("POSITION REDUCTION",
                         f"broker position/order snapshot is malformed ({type(exc).__name__})",
                         intent)

        available = net_quantity - reserved_sells
        requested = self._whole_quantity(
            intent.quantity, field_name="exit quantity")
        if net_quantity <= 0 or available <= 0:
            self._refuse(
                "POSITION REDUCTION",
                "no unreserved positive long position is confirmed for this instrument",
                intent)
        if requested > available:
            self._refuse(
                "POSITION REDUCTION",
                f"requested SELL quantity {requested} exceeds available long quantity "
                f"{available} (position {net_quantity}, pending SELL {reserved_sells})",
                intent)
        return {"position_quantity": net_quantity,
                "reserved_sell_quantity": reserved_sells,
                "available_quantity": available}

    def _authorize_entry_account_limits(self, intent: ExecutionIntent) -> dict:
        """Enforce account-wide gross exposure and open-position caps.

        The software ledger only knows fills this process observed. It is not
        authoritative for a user's other/overnight holdings or working orders,
        so live entries require fresh broker account positions and order-book
        data at the final boundary.
        """
        positions_reader = getattr(self.broker, "get_account_positions", None)
        orders_reader = getattr(self.broker, "get_open_orders", None)
        if not callable(positions_reader) or not callable(orders_reader):
            self._refuse("ACCOUNT RISK",
                         "broker does not expose authoritative account positions "
                         "and working orders", intent)
        try:
            positions = positions_reader()
            orders = orders_reader()
        except Exception as exc:
            self._refuse("ACCOUNT RISK",
                         f"account positions/order book unavailable "
                         f"({type(exc).__name__})", intent)
        if not isinstance(positions, list) or not isinstance(orders, list):
            self._refuse("ACCOUNT RISK",
                         "account positions/order book has an invalid shape", intent)

        position_values: dict[str, float] = {}
        open_instruments: set[str] = set()
        total_exposure = 0.0
        try:
            for row in positions:
                if not isinstance(row, dict):
                    raise ValueError("account position row is malformed")
                instrument = row.get("instrument_token")
                product = row.get("product")
                if (not isinstance(instrument, str) or not instrument.strip()
                        or not isinstance(product, str) or not product.strip()):
                    raise ValueError("account position identity is malformed")
                quantity = self._whole_quantity(
                    row.get("quantity"), field_name="account position quantity",
                    allow_negative=True)
                if quantity == 0:
                    continue
                raw_price = row.get("last_price")
                if raw_price in (None, "", 0, 0.0):
                    raw_price = row.get("average_price")
                if raw_price in (None, "", 0, 0.0):
                    raw_value = row.get("value")
                    try:
                        raw_price = abs(float(raw_value)) / abs(quantity)
                    except (TypeError, ValueError, OverflowError):
                        raw_price = None
                if (isinstance(raw_price, bool)
                        or not isinstance(raw_price, (int, float, str))):
                    raise ValueError("account position has no usable valuation price")
                price = float(raw_price)
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("account position valuation is invalid")
                value = abs(quantity) * price
                position_values[instrument] = position_values.get(instrument, 0.0) + value
                total_exposure += value
                open_instruments.add(instrument)

            pending_values: dict[str, float] = {}
            for row in orders:
                if not isinstance(row, dict):
                    raise ValueError("account order row is malformed")
                instrument = row.get("instrument_token")
                product = row.get("product")
                side = row.get("transaction_type")
                status = row.get("status")
                if (not isinstance(instrument, str) or not instrument.strip()
                        or not isinstance(product, str) or not product.strip()
                        or side not in {"BUY", "SELL"}
                        or not isinstance(status, str) or not status.strip()):
                    raise ValueError("account order identity/status is malformed")
                if side != "BUY" or status.lower() in {
                        "complete", "completed", "rejected", "cancelled", "canceled"}:
                    continue
                pending_raw = row.get("pending_quantity")
                if pending_raw is None:
                    raise ValueError("working BUY order has unknown remaining quantity")
                pending = self._whole_quantity(
                    pending_raw, field_name="working BUY quantity")
                if pending == 0:
                    continue
                price_raw = row.get("price")
                if (isinstance(price_raw, bool)
                        or not isinstance(price_raw, (int, float, str))):
                    raise ValueError("working BUY order has unknown notional")
                price = float(price_raw)
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("working BUY order has unknown notional")
                value = pending * price
                pending_values[instrument] = pending_values.get(instrument, 0.0) + value
                total_exposure += value
                open_instruments.add(instrument)
        except (TypeError, ValueError, OverflowError) as exc:
            self._refuse("ACCOUNT RISK",
                         f"broker account risk snapshot is malformed "
                         f"({type(exc).__name__})", intent)

        order_price = float(intent.price)
        order_value = float(intent.quantity) * order_price
        max_position = getattr(self.settings, "max_position_value", None)
        if (isinstance(max_position, bool)
                or not isinstance(max_position, (int, float))
                or not math.isfinite(float(max_position)) or max_position <= 0):
            self._refuse("ACCOUNT RISK",
                         "MAX_POSITION_VALUE is missing or invalid", intent)
        combined_position = (position_values.get(intent.identity, 0.0)
                             + pending_values.get(intent.identity, 0.0)
                             + order_value)
        if combined_position > float(max_position):
            self._refuse(
                "ACCOUNT RISK",
                f"entry would raise {intent.identity} exposure to "
                f"{combined_position:.2f}, above MAX_POSITION_VALUE "
                f"{float(max_position):.2f}", intent)

        max_positions = getattr(self.settings, "max_open_positions", None)
        if type(max_positions) is not int or max_positions <= 0:
            self._refuse("ACCOUNT RISK",
                         "MAX_OPEN_POSITIONS is missing or invalid", intent)
        if (intent.identity not in open_instruments
                and len(open_instruments) >= max_positions):
            self._refuse("ACCOUNT RISK",
                         f"account already has {len(open_instruments)} open/pending "
                         f"positions (limit {max_positions})", intent)

        total_limit = getattr(self.settings, "max_exposure", None)
        if total_limit is not None:
            if (isinstance(total_limit, bool)
                    or not isinstance(total_limit, (int, float))
                    or not math.isfinite(float(total_limit)) or total_limit <= 0):
                self._refuse("ACCOUNT RISK", "MAX_EXPOSURE is invalid", intent)
            if total_exposure + order_value > float(total_limit):
                self._refuse("ACCOUNT RISK",
                             "entry would exceed the account-wide exposure limit",
                             intent)
        return {"position_value": position_values.get(intent.identity, 0.0),
                "pending_buy_value": pending_values.get(intent.identity, 0.0),
                "total_exposure": total_exposure,
                "open_positions": len(open_instruments),
                "maximum_position_value": float(max_position),
                "maximum_open_positions": max_positions}

    def _require_entry_readiness(self, intent: ExecutionIntent,
                                 lease: dict) -> dict:
        """Revalidate supervised runtime, reconciliation, and market data.

        This is intentionally performed by the final gateway, not inferred
        from a prior GUI/preflight result or process-local READY flag.
        """
        try:
            context = self.sessions._supervisor_context()
            if not isinstance(context, dict):
                raise ValueError("supervisor context is malformed")
            control = self.repository.get_engine_state("live_engine_control")
            runtime = self.repository.get_engine_state("live_engine_runtime")
            if not isinstance(control, dict) or not isinstance(runtime, dict):
                raise ValueError("supervisor state is missing or malformed")
            generation = control.get("generation")
            if (control.get("stop_requested") is not False
                    or type(generation) is not int or generation <= 0):
                raise ValueError("supervisor is stopped or generation is invalid")
            if (context.get("supervisor_verified") is not True
                    or context.get("supervisor_generation") != generation
                    or context.get("supervisor_lease_owner") != lease.get("owner")
                    or context.get("supervisor_pid") != os.getpid()
                    or context.get("supervisor_host") != socket.gethostname()
                    or lease.get("owner") != self.lease_owner
                    or runtime.get("state") != "READY"
                    or runtime.get("owner") != lease.get("owner")
                    or runtime.get("pid") != os.getpid()
                    or runtime.get("host") != socket.gethostname()
                    or runtime.get("generation") != generation
                    or runtime.get("reconciliation_ok") is not True):
                raise ValueError("supervisor lease/runtime identity is not current")
            from app.live.lock import _pid_alive

            if _pid_alive(os.getpid()) is not True:
                raise ValueError("supervised process liveness is unknown")
            silence_minutes = float(getattr(
                self.settings, "live_reconcile_silence_minutes", 10))
            from app.live.readiness import reconciliation_freshness

            reconciliation = reconciliation_freshness(
                self.repository, max_age_seconds=silence_minutes * 60.0,
                now_epoch=self.clock())
            if (reconciliation.get("ok") is not True
                    or runtime.get("reconciled_at")
                    != reconciliation.get("timestamp")):
                raise ValueError(reconciliation.get(
                    "reason", "reconciliation evidence is not current"))

            raw_market_ts = self.repository.get_engine_state("last_market_data_ts")
            if isinstance(raw_market_ts, str):
                parsed = datetime.fromisoformat(
                    raw_market_ts.strip())
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("market-data timestamp has no timezone")
                market_epoch = parsed.timestamp()
            elif (not isinstance(raw_market_ts, bool)
                  and isinstance(raw_market_ts, (int, float))
                  and math.isfinite(float(raw_market_ts))):
                market_epoch = float(raw_market_ts)
            else:
                raise ValueError("market-data timestamp is missing or malformed")
            now = float(self.clock())
            max_market_age = float(getattr(
                self.settings, "live_max_entry_age_s", 30.0))
            market_age = now - market_epoch
            if (not math.isfinite(now) or now <= 0
                    or not math.isfinite(max_market_age) or max_market_age <= 0
                    or not math.isfinite(market_age) or market_age < -1.0
                    or market_age > max_market_age):
                raise ValueError("last market-data evidence is stale or future-dated")
            return {"generation": generation,
                    "reconciled_at": reconciliation.get("timestamp"),
                    "reconciliation_age_seconds": reconciliation.get("age_seconds"),
                    "market_data_age_seconds": max(0.0, market_age)}
        except Exception as exc:
            self._refuse(
                "SUPERVISOR READINESS",
                f"live supervisor/reconciliation/market data could not be verified "
                f"({type(exc).__name__}: {exc})",
                intent)

    def _authorize_live_entry_ops(self, intent: ExecutionIntent) -> None:
        """Re-read token provenance and canary policy at the order boundary."""
        if self.live_ops is None:
            self._refuse("LIVE OPERATIONS",
                         "token/canary guards are unavailable; refusing new risk",
                         intent)
        token_guard = getattr(self.live_ops, "token_guard", None)
        try:
            maximum_age = float(getattr(token_guard, "max_age_hours", 0))
            token = str(getattr(self.settings,
                                "upstox_live_access_token", "") or "")
            token_decision = token_guard.check(
                now=datetime.fromtimestamp(float(self.clock()), UTC),
                credential=token)
        except Exception as exc:
            self._refuse("TOKEN FRESHNESS",
                         f"live credential freshness could not be verified "
                         f"({type(exc).__name__})", intent)
        if (not token.strip() or not math.isfinite(maximum_age)
                or maximum_age <= 0 or not token_decision.allowed):
            reason = (token_decision.reason if not token_decision.allowed else
                      "a non-empty credential and positive freshness window are required")
            self._refuse("TOKEN FRESHNESS", reason, intent)

    def _emergency_stop_state(self, intent: ExecutionIntent) -> tuple[bool, str]:
        try:
            result = self.emergency_stop_reader(self.repository)
            if (not isinstance(result, tuple) or len(result) != 2
                    or type(result[0]) is not bool
                    or not isinstance(result[1], str)):
                return True, "emergency-stop reader returned malformed state"
            return result
        except Exception:
            return True, "emergency-stop flag unreadable"

    def _require_live_lease(self, intent: ExecutionIntent) -> dict:
        from app.live.lock import EngineLock

        try:
            stale_after = int(getattr(
                self.settings, "live_lock_stale_seconds", 180) or 180)
            lease = EngineLock(self.repository, live=True,
                               owner=self.lease_owner,
                               stale_after_seconds=stale_after).status()
        except Exception as exc:
            self._refuse("ENGINE LEASE",
                         f"live-engine lease could not be verified ({type(exc).__name__})",
                         intent)
        age = lease.get("heartbeat_age_seconds")
        stale_limit = lease.get("stale_after_seconds")
        lease_ok = (
            lease.get("readable") is True
            and lease.get("atomic_supported") is True
            and lease.get("held") is True
            and lease.get("live") is True
            and lease.get("owner") == self.lease_owner
            and lease.get("pid") == os.getpid()
            and lease.get("host") == socket.gethostname()
            and lease.get("stale") is False
            and lease.get("owner_liveness") == "alive"
            and isinstance(age, (int, float)) and not isinstance(age, bool)
            and math.isfinite(float(age)) and age >= 0
            and isinstance(stale_limit, (int, float))
            and not isinstance(stale_limit, bool)
            and math.isfinite(float(stale_limit)) and stale_limit > 0
            and age <= stale_limit)
        if not lease_ok:
            self._refuse(
                "ENGINE LEASE",
                "this process does not hold a readable, fresh atomic live-engine "
                "lease; refusing submission",
                intent)
        return lease

    def _require_broker_identity(self, intent: ExecutionIntent) -> None:
        if (str(getattr(self.settings, "mode", "")).upper() != "LIVE"
                or str(getattr(self.settings, "broker", "")).upper() != "UPSTOX_LIVE"
                or getattr(self.settings, "live_trading", False) is not True
                or getattr(self.broker, "LIVE", False) is not True
                or str(getattr(self.broker, "MODE", "")).upper() != "UPSTOX_LIVE"):
            self._refuse("BROKER IDENTITY",
                         "settings and concrete broker do not both prove UPSTOX LIVE",
                         intent)
        base_url = getattr(self.broker, "_base_url", None)
        if base_url is not None:
            from app.config.hosts import LIVE_ORDER_HOSTS, host_of

            if host_of(str(base_url)) not in LIVE_ORDER_HOSTS:
                self._refuse("BROKER IDENTITY",
                             "concrete broker host is not an approved LIVE order host",
                             intent)
        broker_scope = getattr(self.broker, "account_scope", None)
        if ((broker_scope is not None and broker_scope != self.account_scope)
                or not self.account_scope.strip()
                or not isinstance(intent.account_scope, str)
                or not intent.account_scope.strip()
                or intent.account_scope != self.account_scope):
            self._refuse("ACCOUNT SCOPE",
                         "intent account scope is missing or differs from the "
                         "gateway's concrete broker identity", intent)

    def _entry_risk_context(self, intent: ExecutionIntent, state: str,
                            stopped: bool) -> dict:
        if self.guard_chain is None:
            self._refuse("GUARD CHAIN", "required live entry guard chain is unavailable",
                         intent)
        if not callable(self.context_provider):
            self._refuse("RISK CONTEXT",
                         "authoritative live risk context is unavailable", intent)
        try:
            provided = self.context_provider()
            if not isinstance(provided, dict):
                raise TypeError("risk context is not an object")
        except Exception as exc:
            self._refuse("GUARD CHAIN",
                         f"risk context unreadable ({type(exc).__name__}); "
                         "refusing rather than executing with UNKNOWN risk state",
                         intent)
        required = ("current_exposure", "daily_loss_breached",
                    "drawdown_breached", "canary_exhausted")
        missing = [key for key in required if key not in provided]
        if missing:
            self._refuse("RISK CONTEXT",
                         "required live risk evidence is missing: " + ", ".join(missing),
                         intent)
        exposure = provided.get("current_exposure")
        if (isinstance(exposure, bool)
                or not isinstance(exposure, (int, float))
                or not math.isfinite(float(exposure)) or exposure < 0):
            self._refuse("RISK CONTEXT",
                         "current account exposure is malformed or unknown", intent)
        for key in ("daily_loss_breached", "drawdown_breached", "canary_exhausted"):
            if type(provided.get(key)) is not bool:
                self._refuse("RISK CONTEXT",
                             f"{key} must be an explicit boolean", intent)
        context = {"settings": self.settings, "session_state": state,
                   "live_state": state, "signal_id": intent.signal_id,
                   "signal_ts": intent.signal_ts, "intent_id": intent.intent_id,
                   **provided}
        # The gateway's direct, fail-closed reader is authoritative even if a
        # secondary risk-context provider is stale or buggy.
        context["emergency_stop"] = stopped
        return context

    def _evaluate_entry_guards(self, intent: ExecutionIntent, state: str,
                               stopped: bool) -> None:
        context = self._entry_risk_context(intent, state, stopped)
        order = {"side": intent.side, "quantity": intent.quantity,
                 "intent": intent.purpose, "order_type": intent.order_type,
                 "price": intent.price, "symbol": intent.identity,
                 "strategy": intent.strategy_id,
                 "instrument_key": intent.identity}
        verdict = self.guard_chain.evaluate(order, context)
        if not verdict.allowed:
            detail = next((result.detail for result in reversed(verdict.results)
                           if not result.passed), "refused")
            self._refuse("GUARD CHAIN",
                         f"{verdict.refusing_guard}: {detail}", intent)

    def _authorize_broker_mutation(self, broker, intent: ExecutionIntent,
                                   method: str, url: str, *, capability) -> None:
        """Final authorization hook called at the live broker HTTP boundary.

        A valid scope alone is not enough: the immutable intent, concrete
        broker, method and endpoint must agree, then all current live controls
        are re-read after the broker's local journal writes and immediately
        before the request can reach its transport.
        """
        if (broker is not self.broker
                or capability is not _gateway_capability(self)):
            self._refuse("BROKER IDENTITY",
                         "mutating request is not bound to this execution gateway",
                         intent if isinstance(intent, ExecutionIntent) else None)
        safe_intent = intent if isinstance(intent, ExecutionIntent) else None
        problems = (safe_intent.validate() if safe_intent is not None else
                    ["gateway execution intent is missing"])
        if problems:
            self._refuse("INTENT", "; ".join(problems), safe_intent)
        dispatch = _ACTIVE_BROKER_DISPATCH.get()
        if (not isinstance(dispatch, tuple) or len(dispatch) != 4
                or dispatch[0] is not self or dispatch[1] is not broker
                or dispatch[2] is not safe_intent or dispatch[3] is not capability):
            self._refuse(
                "BROKER IDENTITY",
                "mutating request did not originate from this gateway's active submit dispatch",
                safe_intent)

        method = str(method).upper()
        path = urlsplit(str(url)).path
        purpose = intent.purpose
        place = (method == "POST" and path == getattr(broker, "PLACE_PATH", None)
                 and purpose in {"ENTRY", "EXIT", "RISK_REDUCE"})
        cancel = (method == "DELETE"
                  and path == getattr(broker, "CANCEL_PATH", None)
                  and purpose == "CANCEL")
        if not (place or cancel):
            self._refuse("BROKER IDENTITY",
                         "live mutation method/endpoint is not authorized for this intent",
                         intent)
        self._final_revalidate(intent)

    def _final_revalidate(self, intent: ExecutionIntent) -> dict | None:
        """Re-read every safety authorization immediately before the broker.

        Earlier preflight checks improve diagnostics; none is a substitute for
        this second, final check after durable reservations and audit work.
        """
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            try:
                current_hash = configuration_hash(self.settings)
            except Exception as exc:
                self._refuse("CONFIGURATION",
                             f"live risk configuration is invalid "
                             f"({type(exc).__name__})", intent)
            if intent.configuration_hash != current_hash:
                self._refuse("CONFIGURATION",
                             "intent configuration hash differs from current live settings",
                             intent)
            ok, why = self.epochs.validate(
                intent.authorization_epoch, configuration_hash=current_hash)
            if not ok:
                self._refuse("AUTHORIZATION EPOCH", why, intent)

        try:
            snapshot = self.sessions.current()
            state = snapshot.state
        except Exception as exc:
            self._refuse("SESSION STATE",
                         f"current live session could not be verified "
                         f"({type(exc).__name__})", intent)
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES and state != "ACTIVE":
            self._refuse("SESSION STATE",
                         f"final entry check requires ACTIVE; session is {state}", intent)
        if intent.purpose in RISK_MANAGEMENT_PURPOSES and state not in (
                "ARMED", "ACTIVE", "PAUSED", "RECONCILING", "STOPPED", "BLOCKED"):
            self._refuse("SESSION STATE",
                         f"final risk-management check refuses session state {state}",
                         intent)
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            try:
                control = self.repository.get_engine_state("live_engine_control", None)
            except Exception as exc:
                self._refuse("LIVE ENGINE CONTROL",
                             f"final control-state read failed ({type(exc).__name__})",
                             intent)
            if (not isinstance(control, dict)
                    or type(control.get("stop_requested")) is not bool
                    or control.get("stop_requested") is not False
                    or type(control.get("generation")) is not int
                    or control.get("generation", 0) <= 0):
                self._refuse("LIVE ENGINE CONTROL",
                             "live engine is stopping or final control state is malformed",
                             intent)

        lease = self._require_live_lease(intent)
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            self._require_entry_readiness(intent, lease)

        stopped, why = self._emergency_stop_state(intent)
        if stopped and intent.purpose not in EMERGENCY_SAFE_PURPOSES:
            self._refuse("EMERGENCY STOP",
                         f"new risk blocked at final authorization ({why})", intent)
        if str(getattr(self.settings, "live_confirm", "") or "") != LIVE_CONFIRM_PHRASE:
            self._refuse("CONFIRMATION PHRASE",
                         "the configured live confirmation no longer matches "
                         "the required phrase", intent)
        self._require_broker_identity(intent)
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            self._authorize_live_entry_ops(intent)

        reduction_snapshot = self._authorize_position_reduction(intent)
        account_risk_snapshot = None
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            self._evaluate_entry_guards(intent, state, stopped)
            account_risk_snapshot = self._authorize_entry_account_limits(intent)

            # The guard/context provider may take time, and the control plane
            # can change concurrently. Re-read every hard authorization AFTER
            # the last guard and before returning to the broker-call site.
            try:
                current_hash = configuration_hash(self.settings)
            except Exception as exc:
                self._refuse("CONFIGURATION",
                             f"final risk configuration is unreadable "
                             f"({type(exc).__name__})", intent)
            if intent.configuration_hash != current_hash:
                self._refuse("CONFIGURATION",
                             "live configuration changed during final guards", intent)
            ok, why = self.epochs.validate(
                intent.authorization_epoch, configuration_hash=current_hash)
            if not ok:
                self._refuse("AUTHORIZATION EPOCH", why, intent)
            try:
                final_session = self.sessions.current()
                final_control = self.repository.get_engine_state(
                    "live_engine_control", None)
            except Exception as exc:
                self._refuse("SUPERVISOR READINESS",
                             f"final session/control state is unreadable "
                             f"({type(exc).__name__})", intent)
            if (final_session.state != "ACTIVE"
                    or not isinstance(final_control, dict)
                    or final_control.get("stop_requested") is not False
                    or type(final_control.get("generation")) is not int
                    or final_control.get("generation", 0) <= 0):
                self._refuse("SUPERVISOR READINESS",
                             "session or supervisor changed during final guards",
                             intent)
            final_lease = self._require_live_lease(intent)
            self._require_entry_readiness(intent, final_lease)
            final_stopped, final_why = self._emergency_stop_state(intent)
            if final_stopped:
                self._refuse("EMERGENCY STOP",
                             f"new risk blocked at final authorization ({final_why})",
                             intent)
            if str(getattr(self.settings, "live_confirm", "") or "") \
                    != LIVE_CONFIRM_PHRASE:
                self._refuse("CONFIRMATION PHRASE",
                             "the configured live confirmation changed during "
                             "final guards", intent)
            self._require_broker_identity(intent)
            self._authorize_live_entry_ops(intent)
        else:
            # Exits do not depend on entry readiness or an epoch, but the
            # remote position read above can take time; verify the lease and
            # emergency-stop semantics once more after it completes.
            if self.sessions.current().state not in (
                    "ARMED", "ACTIVE", "PAUSED", "RECONCILING", "STOPPED", "BLOCKED"):
                self._refuse("SESSION STATE",
                             "risk-management session changed during final check",
                             intent)
            self._require_live_lease(intent)
            final_stopped, final_why = self._emergency_stop_state(intent)
            if final_stopped and intent.purpose not in EMERGENCY_SAFE_PURPOSES:
                self._refuse("EMERGENCY STOP",
                             f"risk-management action blocked ({final_why})", intent)
            self._require_broker_identity(intent)
        return ({"position_reduction": reduction_snapshot,
                 "account_risk": account_risk_snapshot}
                if intent.purpose in ACTIVE_REQUIRED_PURPOSES
                else reduction_snapshot)

    # ------------------------------------------------------------- submit --
    def submit(self, intent: ExecutionIntent) -> dict:
        """Authorize and (only if every layer passes) submit one live order."""
        self.submission_attempts += 1

        # 1 — structural completeness (§5)
        problems = intent.validate()
        if problems:
            self._refuse("INTENT", "; ".join(problems), intent)

        # 2 — authorization epoch (§6). Exits/cancels act on positions minted
        # under a (possibly now-invalidated) epoch; they are checked against
        # the session state + emergency semantics instead, so an invalidated
        # epoch can never trap a held position.
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            try:
                current_configuration_hash = configuration_hash(self.settings)
            except Exception as exc:
                self._refuse("CONFIGURATION",
                             f"live risk configuration is invalid "
                             f"({type(exc).__name__})", intent)
            ok, why = self.epochs.validate(
                intent.authorization_epoch,
                configuration_hash=current_configuration_hash)
            if not ok:
                self._refuse("AUTHORIZATION EPOCH", why, intent)

        # 3 — persisted session state (§7)
        snapshot = self.sessions.current()
        state = snapshot.state
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES and state != "ACTIVE":
            self._refuse("SESSION STATE",
                         f"entries require ACTIVE; session is {state}", intent)
        if intent.purpose in RISK_MANAGEMENT_PURPOSES and \
                state not in ("ARMED", "ACTIVE", "PAUSED", "RECONCILING",
                              "STOPPED", "BLOCKED"):
            self._refuse("SESSION STATE",
                         f"risk management requires a live session; state is {state}",
                         intent)
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            try:
                control = self.repository.get_engine_state(
                    "live_engine_control", {}) or {}
                if not isinstance(control, dict):
                    raise TypeError("malformed control record")
            except Exception as exc:
                self._refuse("LIVE ENGINE CONTROL",
                             f"live-engine control is unavailable ({type(exc).__name__})",
                             intent)
            if (type(control.get("stop_requested")) is not bool
                    or type(control.get("generation")) is not int
                    or control.get("generation", 0) <= 0):
                self._refuse("LIVE ENGINE CONTROL",
                             "live-engine control state is malformed; new risk is disabled",
                             intent)
            if control.get("stop_requested") is True:
                self._refuse("LIVE ENGINE CONTROL",
                             "live engine is draining; new risk is disabled", intent)

        # 3b — initial live-engine lease and operational evidence.
        lease = self._require_live_lease(intent)
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            self._require_entry_readiness(intent, lease)

        # 4 — emergency-stop semantics: stop NEW RISK, never trap exits.
        stopped, why = self._emergency_stop_state(intent)
        if stopped and intent.purpose not in EMERGENCY_SAFE_PURPOSES:
            self._refuse("EMERGENCY STOP",
                         f"new risk blocked while emergency stop is active ({why})",
                         intent)

        # 5/6 — exact confirmation, concrete broker identity, and account.
        if str(getattr(self.settings, "live_confirm", "") or "") != LIVE_CONFIRM_PHRASE:
            self._refuse("CONFIRMATION PHRASE",
                         "the configured live confirmation does not exactly match "
                         "the required phrase", intent)
        self._require_broker_identity(intent)
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            self._authorize_live_entry_ops(intent)

        # EXIT/RISK_REDUCE require a current broker-confirmed long position.
        reduction_snapshot = self._authorize_position_reduction(intent)

        # Entry rules run now and are re-run after all durable reservations at
        # the final authorization boundary immediately before the broker call.
        if intent.purpose in ACTIVE_REQUIRED_PURPOSES:
            self._evaluate_entry_guards(intent, state, stopped)

        # 8 — duplicate detection (§22 idempotency)
        fingerprint = intent.fingerprint()
        if fingerprint in self._recent_fingerprints and intent.purpose not in {
                "CANCEL", "RECONCILE"}:
            self._refuse("DUPLICATE CHECK",
                         "an identical intent was already authorized recently",
                         intent)

        # Durable cross-process/restart idempotency. Reserve the deterministic
        # intent id before calling any broker. SUBMISSION_ATTEMPTED is written
        # before the remote call; an uncertain outcome is never retried blindly.
        submission_journal = None
        gateway_authorization_id = None
        if intent.purpose not in {"CANCEL", "RECONCILE"}:
            try:
                from app.engine.state import DURABLY_PERSISTED, SubmissionJournal

                if not all(callable(getattr(self.repository, name, None)) for name in (
                        "insert_submission_intent", "get_submission_intent",
                        "list_submission_intents", "update_submission_intent")):
                    self._refuse(
                        "DURABLE INTENT JOURNAL",
                        "permanent keyed SQLite intent storage is unavailable",
                        intent)
                submission_journal = SubmissionJournal(self.repository)
                existing = submission_journal.get(intent.intent_id)
                if existing is not None:
                    same_intent = (
                        existing.get("instrument_key") == intent.identity
                        and existing.get("side") == intent.side
                        and float(existing.get("quantity")) == float(intent.quantity)
                        and float(existing.get("price") or 0.0)
                        == float(intent.price or 0.0))
                    if not same_intent:
                        self._refuse(
                            "DUPLICATE CHECK",
                            "intent id is already durably bound to different order attributes",
                            intent)
                    if existing.get("state") != DURABLY_PERSISTED:
                        self._refuse(
                            "DUPLICATE CHECK",
                            "intent id is already recorded; reconcile it before any retry",
                            intent)
                else:
                    submission_journal.record(
                        intent.intent_id, instrument_key=intent.identity,
                        symbol=intent.identity, side=intent.side,
                        quantity=float(intent.quantity),
                        price=float(intent.price or 0.0),
                        strategy=intent.strategy_id or "risk-management",
                        tag=intent.intent_id[:12])
                if intent.purpose == "ENTRY":
                    canary = getattr(self.live_ops, "canary", None)
                    if canary is None or not callable(
                            getattr(canary, "reserve_submission", None)):
                        self._refuse("CANARY GUARD",
                                     "durable canary reservation is unavailable",
                                     intent)
                    canary_decision = canary.reserve_submission(
                        intent_id=intent.intent_id,
                        quantity=float(intent.quantity),
                        price=intent.price)
                    if not canary_decision.allowed:
                        self._refuse("CANARY GUARD",
                                     canary_decision.reason, intent)
                gateway_authorization_id = uuid4().hex
            except GatewayRefusal:
                raise
            except Exception as exc:
                self._refuse(
                    "DURABLE INTENT JOURNAL",
                    f"submission journal unavailable ({type(exc).__name__}); "
                    "refusing rather than risking a duplicate",
                    intent)

        # 9 — last authorization pass after all reservations. Persist the
        # attempted state only after final checks, but always before the POST.
        try:
            reduction_snapshot = self._final_revalidate(intent)
            if submission_journal is not None:
                from app.engine.state import SUBMISSION_ATTEMPTED

                submission_journal.mark(
                    intent.intent_id, SUBMISSION_ATTEMPTED,
                    detail={"gateway_authorization_id": gateway_authorization_id,
                            "phase": "final_gateway_authorization"})
            # Durable audit is still before the remote call. If writing it
            # fails, the attempted state remains conservative and no POST runs.
            self._audit("LIVE_INTENT_AUTHORIZED",
                        {"intent_id": intent.intent_id, "purpose": intent.purpose,
                         "identity": intent.identity, "side": intent.side,
                         "quantity": intent.quantity,
                         "epoch": intent.authorization_epoch,
                         "strategy_id": intent.strategy_id,
                         "signal_id": intent.signal_id,
                         "configuration_hash": intent.configuration_hash,
                         "build_identity": intent.build_identity,
                         "position_reduction": reduction_snapshot,
                         "cancel_order_id": intent.cancel_order_id})
            # The journal and durable authorization event can take time or
            # trigger fault-injection hooks. Revalidate once more after those
            # writes so there is no database/audit work between the final
            # authorization and the broker call below.
            reduction_snapshot = self._final_revalidate(intent)
        except GatewayRefusal:
            if submission_journal is not None:
                try:
                    from app.engine.state import FINAL_STATE, SUBMISSION_ATTEMPTED

                    entry = submission_journal.get(intent.intent_id)
                    if entry and entry.get("state") == SUBMISSION_ATTEMPTED:
                        submission_journal.mark(
                            intent.intent_id, FINAL_STATE,
                            detail={"outcome": "NOT_SUBMITTED",
                                    "phase": "final_authorization_or_audit_refused"})
                except Exception:
                    # The durable attempted record (or final record if the
                    # write succeeded) continues to forbid a blind retry.
                    pass
            raise
        except Exception as exc:
            self._refuse("FINAL AUTHORIZATION",
                         f"final live authorization failed ({type(exc).__name__})",
                         intent)

        # 10 — final broker operation. A normal order has exactly one submit
        # call. A CANCEL is routed to the broker's cancel method through this
        # same authorization/audit boundary; the facade never exposes a direct
        # cancellation bypass.
        self._recent_fingerprints.append(fingerprint)
        self.broker_calls += 1
        dispatch_token = _ACTIVE_BROKER_DISPATCH.set(
            (self, self.broker, intent, _gateway_capability(self)))
        try:
            try:
                if intent.purpose == "CANCEL":
                    cancel = getattr(self.broker, "cancel", None)
                    if not callable(cancel):
                        self._refuse("BROKER IDENTITY",
                                     "live broker does not expose a cancel operation",
                                     intent)
                    cancel_kwargs = ({"_gateway_capability": _gateway_capability(self),
                                      "_gateway_intent": intent}
                                     if self._broker_capability_bound else {})
                    cancel_result = cancel(str(intent.cancel_order_id), **cancel_kwargs)
                    order_id = str(intent.cancel_order_id)
                elif intent.purpose == "RECONCILE":
                    reconcile = getattr(self.broker, "reconcile", None)
                    if not callable(reconcile):
                        self._refuse("BROKER IDENTITY",
                                     "live broker does not expose reconciliation",
                                     intent)
                    reconcile_result = reconcile()
                    order_id = "reconcile"
                    cancel_result = None
                else:
                    cancel_result = None
                    broker_kwargs = {
                        "order_type": intent.order_type,
                        "intent_id": intent.intent_id,
                        "symbol": intent.identity,
                        "instrument_key": intent.identity,
                        "_gateway_authorization_id": gateway_authorization_id,
                    }
                    if self._broker_capability_bound:
                        broker_kwargs["_gateway_capability"] = _gateway_capability(self)
                        broker_kwargs["_gateway_intent"] = intent
                    order_id = self.broker.submit(
                        intent.side, intent.quantity,
                        intent.price if intent.price is not None else 0,
                        **broker_kwargs)
            except Exception as exc:
                if submission_journal is not None:
                    try:
                        from app.engine.state import FINAL_STATE, UNKNOWN

                        entry = submission_journal.get(intent.intent_id)
                        if entry and entry.get("state") not in {FINAL_STATE, "ACKNOWLEDGED"}:
                            submission_journal.mark(
                                intent.intent_id, UNKNOWN,
                                detail={"error_type": type(exc).__name__,
                                        "phase": "gateway_broker_call"})
                    except Exception:
                        # SUBMISSION_ATTEMPTED was durably written before the
                        # broker call; if this update fails, blind retry remains
                        # blocked by that conservative state.
                        pass
                self._audit("LIVE_INTENT_FAILED",
                            {"intent_id": intent.intent_id,
                             "purpose": intent.purpose,
                             "error_type": type(exc).__name__})
                raise
        finally:
            _ACTIVE_BROKER_DISPATCH.reset(dispatch_token)
        if submission_journal is not None:
            try:
                from app.engine.state import ACKNOWLEDGED, SUBMISSION_ATTEMPTED

                entry = submission_journal.get(intent.intent_id)
                if entry and entry.get("state") == SUBMISSION_ATTEMPTED:
                    submission_journal.mark(
                        intent.intent_id, ACKNOWLEDGED,
                        detail={"order_id": str(order_id),
                                "phase": "gateway_broker_acknowledged"})
            except Exception:
                # The attempted state was persisted before the remote call and
                # still forbids retries if ACKNOWLEDGED cannot be recorded.
                pass

        if intent.purpose == "CANCEL":
            self._audit("LIVE_ORDER_CANCEL_RESULT",
                        {"intent_id": intent.intent_id,
                         "target_order_id": str(intent.cancel_order_id),
                         "result": cancel_result})
            return {"authorized": True, "submitted": False,
                    "cancelled": cancel_result, "order_id": order_id,
                    "intent_id": intent.intent_id, "purpose": intent.purpose,
                    "epoch": intent.authorization_epoch}
        if intent.purpose == "RECONCILE":
            self._audit("LIVE_RECONCILIATION_RESULT",
                        {"intent_id": intent.intent_id,
                         "result_count": (len(reconcile_result)
                                          if hasattr(reconcile_result, "__len__")
                                          else None)})
            return {"authorized": True, "submitted": False,
                    "reconciled": reconcile_result,
                    "intent_id": intent.intent_id, "purpose": intent.purpose,
                    "epoch": intent.authorization_epoch}
        self._audit("LIVE_INTENT_SUBMITTED",
                    {"intent_id": intent.intent_id, "order_id": str(order_id),
                     "purpose": intent.purpose})
        return {"authorized": True, "submitted": True, "order_id": str(order_id),
                "intent_id": intent.intent_id, "purpose": intent.purpose,
                "epoch": intent.authorization_epoch}


def configuration_hash(settings) -> str:
    """Hash all live-risk and routing settings without serializing secrets."""
    names = (
        "mode", "broker", "live_trading", "data_source", "market_calendar",
        "market_timezone", "market_universe", "default_timeframe",
        "max_drawdown", "max_daily_loss", "max_position_value",
        "max_open_positions", "max_trades_per_day",
        "live_max_equity_drawdown", "live_canary_max_notional",
        "live_canary_orders", "live_token_max_age_hours",
        "live_reconcile_silence_minutes", "live_lock_stale_seconds",
        "live_max_entry_age_s", "live_product", "upstox_live_base_url",
        "upstox_live_order_history_url", "live_rate_limit_profile",
    )
    values = {}
    for name in names:
        value = getattr(settings, name, None)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"live configuration field {name} is non-finite")
        values[name] = value
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"),
                         allow_nan=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
