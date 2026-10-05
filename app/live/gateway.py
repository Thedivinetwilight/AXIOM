"""The single authoritative live execution gateway (FINAL-11/10 §3–§13).

One source of truth decides whether a REAL-MONEY order may leave the
building. Nothing else in the codebase may submit to a live broker —
the CLI, the GUI, research, recovery and background jobs all arrive here
or do not happen at all.

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
import os
import socket
import time
from dataclasses import dataclass, field, asdict
from uuid import uuid4

from app.config.settings import LIVE_CONFIRM_PHRASE

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
        if not str(self.market_id).strip():
            problems.append("market_id is required")
        if not str(self.identity).strip():
            problems.append("identity is required")
        if self.side not in ("BUY", "SELL"):
            problems.append("side must be BUY or SELL")
        if not isinstance(self.quantity, (int, float)) or self.quantity <= 0:
            problems.append("quantity must be positive")
        if self.purpose == "ENTRY":
            for name in self.REQUIRED_FOR_ENTRY:
                value = getattr(self, name)
                if value is None or value == "" or value == 0:
                    problems.append(f"ENTRY intent requires {name}")
            if self.authorization_epoch is None:
                problems.append("ENTRY intent requires an authorization epoch")
        if self.purpose == "CANCEL" and not str(self.cancel_order_id or "").strip():
            problems.append("CANCEL intent requires the target order id")
        if not problems and self.purpose == "ENTRY" and self.side == "SELL" \
                and self.quantity and not self.universe:
            # An entry SELL without portfolio context cannot be risk-checked
            # for exposure — refuse rather than assume.
            problems.append("ENTRY SELL requires universe (exposure context)")
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
        return state if isinstance(state, dict) else None

    def _save(self, state: dict) -> None:
        if self.repository is not None:
            self.repository.set_engine_state(EPOCH_KEY, state)

    def current(self) -> dict | None:
        return self._load()

    def activate(self, *, actor: str = "gui", session_state: str = "ACTIVE") -> dict:
        """Mint a fresh epoch, invalidating any previous one."""
        previous = self._load() or {}
        was_valid = bool(previous.get("valid"))
        state = {
            "epoch": int(previous.get("epoch", 0)) + 1 if previous else 1,
            "activated_ts": self.clock(),
            "actor": str(actor),
            "session_state": session_state,
            "valid": True,
            "superseded_epochs": int(previous.get("superseded_epochs", 0))
            + (1 if was_valid else 0),
        }
        self._save(state)
        return state

    def invalidate(self, *, reason: str) -> dict | None:
        state = self._load()
        if state is None:
            return None
        state = {**state, "valid": False,
                 "invalidated_ts": self.clock(),
                 "invalidation_reason": str(reason)}
        self._save(state)
        return state

    def validate(self, epoch: int | None) -> tuple[bool, str]:
        """True only for the CURRENT epoch while it remains valid."""
        state = self._load()
        if state is None:
            return False, "no authorization epoch exists (live never activated here)"
        if not state.get("valid"):
            return False, ("authorization epoch was invalidated "
                           f"({state.get('invalidation_reason', 'no reason recorded')})")
        if epoch is None:
            return False, "intent carries no authorization epoch"
        if int(epoch) != int(state.get("epoch", -1)):
            return False, (f"stale authorization epoch {epoch}; current is "
                           f"{state.get('epoch')}")
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
                 clock=time.time, memory: int = 256):
        self.settings = settings
        self.repository = repository
        self.sessions = session_manager
        self.broker = broker
        self.guard_chain = guard_chain
        self.epochs = epochs or AuthorizationEpochs(repository, clock=clock)
        self.emergency_stop_reader = emergency_stop_reader
        self.account_scope = str(account_scope or "")
        from app.live.lock import owner_id

        # A real-money submission must be made by the process that owns the
        # current durable live-engine lease. Default identity is host+PID;
        # tests/supervisors may pass the exact owner explicitly.
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
                                 f"audit could not be written ({exc}); "
                                 "refusing rather than executing unrecorded")

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
            ok, why = self.epochs.validate(intent.authorization_epoch)
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
            if control.get("stop_requested") is True:
                self._refuse("LIVE ENGINE CONTROL",
                             "live engine is draining; new risk is disabled", intent)

        # 3b — live-engine lease (§15). This is re-read at the FINAL live
        # submission boundary; an ACTIVE database state or stale process-local
        # boolean is not authority. A held position can still exit after an
        # operator STOP, provided this same engine retains its healthy lease.
        from app.live.lock import EngineLock

        lease = EngineLock(self.repository, live=True,
                           owner=self.lease_owner).status()
        age = lease.get("heartbeat_age_seconds")
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
            and isinstance(age, (int, float))
            and age <= lease.get("stale_after_seconds", 0))
        if not lease_ok:
            self._refuse(
                "ENGINE LEASE",
                "this process does not hold a readable, fresh atomic live-engine "
                "lease; refusing submission",
                intent)

        # 4 — emergency-stop semantics (§12): stop NEW RISK, never trap exits
        if self.emergency_stop_reader is not None:
            try:
                stopped, why = self.emergency_stop_reader(self.repository)
            except Exception:
                stopped, why = True, "emergency-stop flag unreadable"
            if stopped and intent.purpose not in EMERGENCY_SAFE_PURPOSES:
                self._refuse("EMERGENCY STOP",
                             f"new risk blocked while emergency stop is active ({why})",
                             intent)

        # 5 — exact confirmation phrase, gateway layer (§13)
        configured = str(getattr(self.settings, "live_confirm", "") or "")
        if configured != LIVE_CONFIRM_PHRASE:
            self._refuse("CONFIRMATION PHRASE",
                         "the configured live confirmation does not exactly match "
                         "the required phrase", intent)

        # 6 — broker identity & account scope (§4, §16)
        if not getattr(self.broker, "LIVE", False):
            self._refuse("BROKER IDENTITY",
                         "the gateway refuses to route a live intent through a "
                         "non-live broker", intent)
        if self.account_scope and intent.account_scope and \
                intent.account_scope != self.account_scope:
            self._refuse("ACCOUNT SCOPE",
                         f"intent account {intent.account_scope!r} does not match "
                         f"gateway account {self.account_scope!r}", intent)

        # 7 — entry guard chain is the real final gate (§10). These checks
        # are intentionally ENTRY-only: stale alpha, liquidity, model, or
        # sizing context must never prevent cancellation or a risk-reducing
        # exit from an already-held position.
        if intent.purpose == "ENTRY" and self.guard_chain is not None:
            order = {"side": intent.side, "quantity": intent.quantity,
                     "intent": intent.purpose, "order_type": intent.order_type,
                     "price": intent.price, "symbol": intent.identity,
                     "strategy": intent.strategy_id,
                     "instrument_key": intent.identity.split("|")[-1]}
            context = {"settings": self.settings, "session_state": state,
                       "live_state": state, "signal_id": intent.signal_id,
                       "signal_ts": intent.signal_ts,
                       "intent_id": intent.intent_id,
                       "emergency_stop": False}
            if self.context_provider is not None:
                try:
                    context.update(self.context_provider())
                except Exception as exc:
                    # Risk state UNKNOWN -> fail closed for any decision that
                    # adds risk. Exits/cancels do not depend on this context.
                    self._refuse("GUARD CHAIN",
                                 f"risk context unreadable ({type(exc).__name__}); "
                                 "refusing rather than executing with UNKNOWN risk state",
                                 intent)
            verdict = self.guard_chain.evaluate(order, context)
            if not verdict.allowed:
                detail = next((r.detail for r in reversed(verdict.results)
                               if not r.passed), "refused")
                self._refuse("GUARD CHAIN",
                             f"{verdict.refusing_guard}: {detail}", intent)

        # 8 — duplicate detection (§22 idempotency)
        fingerprint = intent.fingerprint()
        if fingerprint in self._recent_fingerprints and intent.purpose not in {
                "CANCEL", "RECONCILE"}:
            self._refuse("DUPLICATE CHECK",
                         "an identical intent was already authorized recently",
                         intent)

        # 9 — durable audit BEFORE the remote call (§24 observability)
        self._audit("LIVE_INTENT_AUTHORIZED",
                    {"intent_id": intent.intent_id, "purpose": intent.purpose,
                     "identity": intent.identity, "side": intent.side,
                     "quantity": intent.quantity,
                     "epoch": intent.authorization_epoch,
                     "strategy_id": intent.strategy_id,
                     "signal_id": intent.signal_id,
                     "configuration_hash": intent.configuration_hash,
                     "build_identity": intent.build_identity,
                     "cancel_order_id": intent.cancel_order_id})

        # 10 — final broker operation. A normal order has exactly one submit
        # call. A CANCEL is routed to the broker's cancel method through this
        # same authorization/audit boundary; the facade never exposes a direct
        # cancellation bypass.
        self._recent_fingerprints.append(fingerprint)
        self.broker_calls += 1
        try:
            if intent.purpose == "CANCEL":
                cancel = getattr(self.broker, "cancel", None)
                if not callable(cancel):
                    self._refuse("BROKER IDENTITY",
                                 "live broker does not expose a cancel operation",
                                 intent)
                cancel_result = cancel(str(intent.cancel_order_id))
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
                order_id = self.broker.submit(
                    intent.side, intent.quantity,
                    intent.price if intent.price is not None else 0,
                    order_type=intent.order_type, intent_id=intent.intent_id,
                    symbol=intent.identity, instrument_key=intent.identity)
        except Exception as exc:
            self._audit("LIVE_INTENT_FAILED",
                        {"intent_id": intent.intent_id,
                         "purpose": intent.purpose,
                         "error_type": type(exc).__name__})
            raise
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
    """Deterministic hash of the live-relevant configuration (§5)."""
    payload = json.dumps({
        "mode": getattr(settings, "mode", ""),
        "broker": getattr(settings, "broker", ""),
        "live_trading": bool(getattr(settings, "live_trading", False)),
        "data_source": getattr(settings, "data_source", ""),
        "max_drawdown": getattr(settings, "max_drawdown", None),
        "max_daily_loss": getattr(settings, "max_daily_loss", None),
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
