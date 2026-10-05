"""Live session state machine (master spec sections 31-37, 135-137).

Live trading is a FIRST-CLASS mode — not a permanently hard-coded-off
boolean — but it stays fail-closed. The machine separates:

    OFF          no live configuration present
    CONFIGURED   the documented multi-switch opt-in is present
    READY        the full readiness contract passes
    ARMED        the user explicitly authorized activation (durable)
    ACTIVE       authorized to submit real orders
    PAUSED       new entries suspended by the operator
    RECONCILING  broker reconciliation in progress (entries suspended)
    STOPPED      session stopped; authorization cleared
    BLOCKED      a hard condition (emergency stop, breach, reconciliation
                 mismatch) is preventing operation

Hard rules enforced here and by tests:

* nothing in installation, setup, restart, UI reopen, research, model
  installation, configuration change, token refresh or recovery may move the
  machine into ARMED or ACTIVE by itself;
* ARMING requires the readiness contract to pass AND the operator's explicit
  confirmation phrase — the same phrase the historical gate required;
* every transition is persisted append-only (audit) and published on the bus;
* BLOCKED and emergency-stop conditions beat every other transition;
* exits (risk-reducing closes) are never blocked by an entry-path failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config.settings import LIVE_CONFIRM_PHRASE
from app.live import preflight as live_preflight

logger = logging.getLogger("bot.live.session")

OFF = "OFF"
CONFIGURED = "CONFIGURED"
READY = "READY"
ARMED = "ARMED"
ACTIVE = "ACTIVE"
PAUSED = "PAUSED"
RECONCILING = "RECONCILING"
STOPPED = "STOPPED"
BLOCKED = "BLOCKED"

LIVE_STATES = (OFF, CONFIGURED, READY, ARMED, ACTIVE, PAUSED, RECONCILING,
               STOPPED, BLOCKED)

# Where the machine may go from each state without an operator action.
# Operator transitions are checked explicitly by method, never by table walk.
# The table lists structurally-possible transitions; OPERATOR transitions
# (arm/activate/stop) additionally enforce phrase/readiness/state rules in
# their methods. ARMED is reachable from OFF and STOPPED because arming is
# always a fresh, explicit authorization — never an automatic one.
_AUTOMATIC = {
    OFF: (CONFIGURED, ARMED),
    CONFIGURED: (READY, BLOCKED, OFF),
    READY: (ARMED, CONFIGURED, BLOCKED, OFF),
    ARMED: (ACTIVE, READY, BLOCKED, STOPPED),
    ACTIVE: (PAUSED, RECONCILING, BLOCKED, STOPPED),
    PAUSED: (ACTIVE, RECONCILING, BLOCKED, STOPPED),
    RECONCILING: (ACTIVE, PAUSED, BLOCKED, STOPPED),
    STOPPED: (OFF, CONFIGURED, READY, ARMED),
    BLOCKED: (STOPPED, READY, CONFIGURED, OFF),
}


class LiveTransitionError(RuntimeError):
    """Raised when a requested transition violates the state machine."""


@dataclass(frozen=True)
class LiveSnapshot:
    state: str
    reason: str
    ts_entered: str | None

    def to_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason,
                "ts_entered": self.ts_entered}


class LiveSessionManager:
    def __init__(self, repository, bus=None, *, engine_supervisor=None):
        self.repository = repository
        self.bus = bus
        self.engine_supervisor = engine_supervisor
        self._last_settings = None
        # Authorization epochs (§6 FINAL-11/10): activation mints a fresh
        # epoch; stop/disarm/block invalidate it. Durable, so a stale engine
        # process cannot reuse a superseded authorization.
        from app.live.gateway import AuthorizationEpochs
        self.epochs = AuthorizationEpochs(repository) if repository is not None else None

    def _supervisor_context(self) -> dict:
        """Prove the real engine process owns a reconciled live lease."""
        if self.engine_supervisor is not None:
            try:
                return self.engine_supervisor.readiness_context()
            except Exception as exc:
                raise LiveTransitionError(
                    "the supervised live engine is not ready "
                    f"({type(exc).__name__}); activation is refused") from exc
        if self.repository is None:
            raise LiveTransitionError(
                "activation requires a repository-backed live-engine supervisor")
        from app.live.lock import EngineLock, _pid_alive

        lease = EngineLock(self.repository, live=True).status()
        try:
            runtime = self.repository.get_engine_state("live_engine_runtime", {}) or {}
        except Exception as exc:
            raise LiveTransitionError(
                "supervised runtime state is unreadable; activation is refused") from exc
        age = lease.get("heartbeat_age_seconds")
        verified = (
            lease.get("readable") is True
            and lease.get("atomic_supported") is True
            and lease.get("held") is True and lease.get("live") is True
            and lease.get("stale") is False
            and lease.get("owner_liveness") == "alive"
            and isinstance(age, (int, float))
            and age <= lease.get("stale_after_seconds", 0)
            and _pid_alive(int(lease.get("pid") or 0)) is True
            and isinstance(runtime, dict) and runtime.get("state") == "READY"
            and runtime.get("owner") == lease.get("owner")
            and runtime.get("pid") == lease.get("pid")
            and runtime.get("host") == lease.get("host")
            and bool(runtime.get("reconciled_at")))
        if not verified:
            raise LiveTransitionError(
                "a live engine with a fresh lease and completed reconciliation "
                "is required before activation")
        return {"supervisor_verified": True,
                "supervisor_lease_owner": lease["owner"],
                "supervisor_pid": lease["pid"],
                "supervisor_host": lease["host"],
                "last_market_data_ts": self.repository.get_engine_state(
                    "last_market_data_ts")}

    # ------------------------------------------------------------ current --
    def current(self) -> LiveSnapshot:
        row = self.repository.current_live_session() if self.repository else None
        if row is None:
            return LiveSnapshot(OFF, "no live session recorded", None)
        return LiveSnapshot(row["state"], row.get("reason", ""), row.get("ts_entered"))

    def history(self, limit: int = 50) -> list[dict]:
        return self.repository.list_live_sessions(limit=limit) if self.repository else []

    # --------------------------------------------------- derived position --
    def derived_state(self, settings, *, repository=None, emergency_stop: bool = False,
                      reconciliation_blocked: bool = False) -> str:
        """Where the machine SHOULD sit given configuration + readiness.

        This never mutates state; it is the truth the GUI shows before any
        explicit operator authorization exists.
        """
        if emergency_stop:
            return BLOCKED
        configured = self._configuration_complete(settings)
        if not configured:
            return OFF if not self._any_live_intent(settings) else CONFIGURED
        context = {}
        if self.engine_supervisor is not None:
            try:
                context = self.engine_supervisor.readiness_context()
            except Exception:
                # A failed supervisor proof remains visible as BLOCKED in the
                # readiness contract whenever a live lease is held; never
                # synthesize a ready context for the UI.
                context = {}
        readiness = self.readiness(
            settings, repository=repository or self.repository, context=context)
        if reconciliation_blocked:
            return RECONCILING
        if readiness["verdict"] in ("BLOCKED", "BLOCKED_UNVERIFIED"):
            return BLOCKED
        return READY

    @staticmethod
    def _any_live_intent(settings) -> bool:
        return bool(getattr(settings, "live_trading", False)) or \
            (getattr(settings, "mode", "") or "").upper() == "LIVE" or \
            (getattr(settings, "broker", "") or "").upper() == "UPSTOX_LIVE"

    @staticmethod
    def _configuration_complete(settings) -> bool:
        """The documented multi-switch opt-in must ALL agree (spec: fail-closed)."""
        mode = (getattr(settings, "mode", "") or "").upper()
        broker = (getattr(settings, "broker", "") or "").upper()
        confirm = (getattr(settings, "live_confirm", "") or "")
        return (mode == "LIVE" and broker == "UPSTOX_LIVE"
                and bool(getattr(settings, "live_trading", False))
                and confirm == LIVE_CONFIRM_PHRASE)

    # -------------------------------------------------------- readiness ---
    def readiness(self, settings, *, repository=None,
                  context: dict | None = None) -> dict:
        """Full operational readiness, including UNKNOWN-as-blocking checks."""
        checks = live_preflight.run_readiness_center_checks(
            settings, repository=repository, context=context)
        serialized = []
        for check in checks:
            if check.ok:
                status = "READY"
            elif check.severity == live_preflight.ERROR:
                status = "BLOCKED"
            elif check.severity == live_preflight.WARNING:
                status = "WARNING"
            else:
                # INFO severity with ok=False means "not checked" — it must
                # never be readable as healthy (spec section 32).
                status = "UNKNOWN"
            entry = check.to_dict()
            entry["status"] = status
            serialized.append(entry)
        blocked = [c for c in serialized if c["status"] == "BLOCKED"]
        unknown = [c for c in serialized if c["status"] == "UNKNOWN"]
        warnings = [c for c in serialized if c["status"] == "WARNING"]
        # FINAL-11/10 §8: an UNKNOWN REQUIRED safety check is not healthy —
        # it blocks arming exactly like a BLOCKED one. Optional (marked
        # informational) checks may stay UNKNOWN without blocking.
        required_blocked = [c for c in blocked if c.get("required", True)]
        required_unknown = [c for c in unknown if c.get("required", True)]
        # Verdict semantics follow the preflight design: ERROR-severity
        # failures block; WARNINGs are accepted-known gaps; INFO "not
        # checked" items (fresh database, no recorded state yet) never fail
        # the verdict on their own but are ALWAYS listed as UNKNOWN — they
        # can never be read as verified-clean (spec section 32: a missing
        # check must never silently become healthy).
        if blocked:
            verdict = "BLOCKED"
        elif required_unknown:
            # UNKNOWN on a REQUIRED check is not arming-healthy. It gets its
            # own verdict so it can never be read as "ready with a caveat".
            verdict = "BLOCKED_UNVERIFIED"
        elif warnings:
            verdict = "READY_WITH_WARNINGS"
        elif unknown:
            verdict = "READY_WITH_UNVERIFIED"
        else:
            verdict = "READY"
        return {
            "verdict": verdict,
            "checks": serialized,
            "required_phrase": LIVE_CONFIRM_PHRASE,
            "counts": {"ready": len(serialized) - len(blocked) - len(unknown) - len(warnings),
                       "warning": len(warnings), "blocked": len(blocked),
                       "unknown": len(unknown),
                       "required_blocked": len(required_blocked),
                       "required_unknown": len(required_unknown)},
            "required_blocked": [c["name"] for c in required_blocked],
            "required_unknown": [c["name"] for c in required_unknown],
        }

    # ------------------------------------------------------- transitions --
    def arm(self, settings, *, confirmation_phrase: str, repository=None,
            actor: str = "gui", context: dict | None = None) -> LiveSnapshot:
        """Explicit operator authorization: READY -> ARMED.

        Refuses unless the readiness contract passes. This is the ONLY path
        into ARMED; no timer, restart handler, research hook or config change
        calls this method.
        """
        if confirmation_phrase != LIVE_CONFIRM_PHRASE:
            raise LiveTransitionError(
                "Arming live trading requires the explicit confirmation phrase.")
        current = self.current()
        if current.state not in (OFF, CONFIGURED, READY, STOPPED):
            raise LiveTransitionError(
                f"Cannot arm live trading from state {current.state}; "
                "resolve or explicitly release the current session first.")
        if self.repository is not None:
            from app.live.ops import read_emergency_stop

            try:
                stopped, why = read_emergency_stop(self.repository)
            except Exception:
                stopped, why = True, "emergency-stop flag unreadable"
            if stopped:
                raise LiveTransitionError(
                    f"Cannot arm while the emergency stop is active ({why}).")
        # A real supervisor/lease must exist before the state can be armed;
        # an ACTIVE/READY database value alone is not runtime proof.
        context = self._supervisor_context()
        readiness = self.readiness(
            settings, repository=repository or self.repository, context=context)
        if readiness["verdict"] in ("BLOCKED", "BLOCKED_UNVERIFIED"):
            offenders = readiness["required_blocked"] + readiness["required_unknown"]
            raise LiveTransitionError(
                "Readiness contract refuses arming: "
                + "; ".join(offenders or ["unknown blocker"])
                + (" — a required check is UNKNOWN and must block"
                   if readiness["verdict"] == "BLOCKED_UNVERIFIED" else ""))
        self._last_settings = settings
        return self._transition(ARMED, f"operator arming by {actor} after readiness "
                                       f"verdict {readiness['verdict']}",
                                payload={"verdict": readiness["verdict"]})

    def activate(self, *, settings=None, actor: str = "gui",
                 context: dict | None = None) -> LiveSnapshot:
        """ARMED -> ACTIVE after the COMPLETE readiness contract is rechecked.

        Activation requires prior explicit arming, never from OFF/READY. It
        mints a FRESH authorization epoch; the engine/supervisor context is
        supplied by the trusted workstation service, not by the request body.
        """
        current = self.current()
        if current.state != ARMED:
            raise LiveTransitionError(
                f"Cannot activate live trading from state {current.state}; "
                "explicit arming is required first.")
        settings = settings or self._last_settings
        if settings is None:
            raise LiveTransitionError(
                "Activation cannot re-verify readiness without current settings.")
        context = self._supervisor_context()
        readiness = self.readiness(
            settings, repository=self.repository, context=context)
        if readiness["verdict"] in ("BLOCKED", "BLOCKED_UNVERIFIED"):
            offenders = readiness["required_blocked"] + readiness["required_unknown"]
            raise LiveTransitionError(
                "Activation refused by the current readiness contract: "
                + "; ".join(offenders or ["unknown blocker"]))
        self._last_settings = settings
        snapshot = self._transition(ACTIVE, f"operator start by {actor}",
                                    payload={"readiness": readiness["verdict"]})
        if self.epochs is not None:
            self.epochs.activate(actor=actor)
        return snapshot

    def pause(self, *, reason: str = "operator pause") -> LiveSnapshot:
        current = self.current()
        if current.state not in (ACTIVE, RECONCILING):
            raise LiveTransitionError(f"Cannot pause from state {current.state}")
        return self._transition(PAUSED, reason)

    def resume(self, *, settings=None, context: dict | None = None) -> LiveSnapshot:
        current = self.current()
        if current.state != PAUSED:
            raise LiveTransitionError(f"Cannot resume from state {current.state}")
        settings = settings or self._last_settings
        if settings is None:
            raise LiveTransitionError(
                "Resume cannot re-verify readiness without current settings.")
        context = self._supervisor_context()
        readiness = self.readiness(
            settings, repository=self.repository, context=context)
        if readiness["verdict"] in ("BLOCKED", "BLOCKED_UNVERIFIED"):
            offenders = readiness["required_blocked"] + readiness["required_unknown"]
            raise LiveTransitionError(
                "Resume refused by the current readiness contract: "
                + "; ".join(offenders or ["unknown blocker"]))
        self._last_settings = settings
        snapshot = self._transition(ACTIVE, "operator resume",
                                    payload={"readiness": readiness["verdict"]})
        if self.epochs is not None:
            # Resuming IS an activation: fresh epoch, stale intents refused.
            self.epochs.activate(actor="resume")
        return snapshot

    def begin_reconciliation(self, *, reason: str = "reconciliation watchdog") -> LiveSnapshot:
        current = self.current()
        if current.state not in (ACTIVE, PAUSED):
            raise LiveTransitionError(f"Cannot reconcile from state {current.state}")
        return self._transition(RECONCILING, reason)

    def finish_reconciliation(self, *, outcome: str) -> LiveSnapshot:
        current = self.current()
        if current.state != RECONCILING:
            raise LiveTransitionError(f"Not reconciling (state {current.state})")
        if outcome == "clean":
            return self._transition(ACTIVE, "reconciliation completed clean")
        return self._transition(BLOCKED, f"reconciliation ended with: {outcome}")

    def stop(self, *, reason: str = "operator stop", keep_configuration: bool = True) -> LiveSnapshot:
        """Explicit stop. Clears authorization; configuration may remain.

        The durable stop request lets the child drain when its position/order
        book is flat. If any risk remains it retains its lease and continues
        risk-reducing management; the control plane never kills it.
        """
        current = self.current()
        if current.state == OFF:
            snapshot = current
        else:
            snapshot = self._transition(STOPPED, reason)
        if self.epochs is not None:
            self.epochs.invalidate(reason=f"session stopped: {reason}")
        self._request_supervisor_stop(reason)
        if not keep_configuration and snapshot.state != OFF:
            self._transition(OFF, "configuration cleared after stop")
            return self.current()
        return snapshot

    def disarm(self, *, reason: str = "operator disarm") -> LiveSnapshot:
        current = self.current()
        if current.state in (OFF, STOPPED):
            self._request_supervisor_stop(reason)
            return current
        if current.state in (ACTIVE, PAUSED, RECONCILING):
            raise LiveTransitionError(
                f"Stop the live session before disarming (state {current.state})")
        snapshot = self._transition(STOPPED, reason)
        if self.epochs is not None:
            self.epochs.invalidate(reason=f"session disarmed: {reason}")
        self._request_supervisor_stop(reason)
        return snapshot

    def block(self, *, reason: str) -> LiveSnapshot:
        """Hard conditions (emergency stop, breach) force BLOCKED from anywhere."""
        snapshot = self._transition(BLOCKED, reason)
        if self.epochs is not None:
            self.epochs.invalidate(reason=f"session blocked: {reason}")
        self._request_supervisor_stop(reason)
        return snapshot

    def release_block(self, *, destination: str | None = None,
                      reason: str = "block released") -> LiveSnapshot:
        current = self.current()
        if current.state != BLOCKED:
            raise LiveTransitionError(f"Not blocked (state {current.state})")
        try:
            from app.live.ops import read_emergency_stop

            stopped, why = read_emergency_stop(self.repository)
        except Exception:
            stopped, why = True, "emergency-stop state unavailable"
        if stopped:
            raise LiveTransitionError(
                f"Cannot release the block while the emergency stop is active ({why}).")
        target = destination or READY
        if target not in _AUTOMATIC[BLOCKED]:
            raise LiveTransitionError(f"BLOCKED cannot be released to {target}")
        if target == ARMED or target == ACTIVE:
            raise LiveTransitionError(
                "Releasing a block can never re-arm or re-activate directly; "
                "operator authorization is required again.")
        snapshot = self._transition(target, reason)
        self._request_supervisor_stop(
            "emergency stop cleared; fresh ARM and ACTIVATE are required")
        return snapshot

    def recover_after_restart(self) -> LiveSnapshot:
        """Recover conservatively when the operator control plane restarts.

        A prior explicit ACTIVE state is not carried through a control-plane
        restart. The engine is moved to STOPPED, its authorization epoch is
        invalidated, and any surviving child is asked to drain. Existing
        positions remain managed by the child through risk-reducing paths.
        No startup recovery path can create ARMED or ACTIVE.
        """
        current = self.current()
        if current.state in (ARMED, ACTIVE, PAUSED, RECONCILING):
            current = self._transition(
                STOPPED,
                "control-plane restart requires a fresh explicit ARM cycle")
            if self.epochs is not None:
                self.epochs.invalidate(reason="control-plane restart recovery")
        self._request_supervisor_stop("control-plane startup recovery")
        return current

    def _request_supervisor_stop(self, reason: str) -> None:
        supervisor = self.engine_supervisor
        if supervisor is None:
            return
        try:
            supervisor.request_stop(reason=reason)
        except Exception as exc:
            # The durable session state above independently blocks new entries.
            # Surface the failed notification for diagnosis; never roll the
            # safety transition back or replace it with a force-kill.
            try:
                self.repository.record_system_event(
                    "LIVE_ENGINE_STOP_SIGNAL_FAILED",
                    {"reason": str(reason), "error_type": type(exc).__name__})
            except Exception as audit_exc:
                logger.error("LIVE_ENGINE_STOP_FAILURE_AUDIT_UNAVAILABLE",
                             extra={"error_type": type(audit_exc).__name__})

    # -------------------------------------------------------------- start --
    def start_confirmation_summary(self, settings, *, repository=None,
                                   emergency_stop: bool = False) -> dict:
        """The deliberate pre-start summary required by spec section 33."""
        readiness = self.readiness(settings, repository=repository)
        risk = {
            "max_daily_loss": getattr(settings, "max_daily_loss", None),
            "max_position_value": getattr(settings, "max_position_value", None),
            "max_exposure": getattr(settings, "max_exposure", None),
        }
        return {
            "mode": getattr(settings, "mode", None),
            "broker": getattr(settings, "broker", None),
            "market": getattr(settings, "market", "india"),
            "risk_limits": risk,
            "emergency_stop_active": bool(emergency_stop),
            "readiness_verdict": readiness["verdict"],
            "blocked_checks": [c["name"] for c in readiness["checks"]
                               if c["status"] == "BLOCKED"],
            "unknown_checks": [c["name"] for c in readiness["checks"]
                               if c["status"] == "UNKNOWN"],
            "required_phrase": LIVE_CONFIRM_PHRASE,
            "warning": "Starting a live session can place REAL orders with REAL "
                       "money once activated. Nothing starts until you arm and "
                       "then activate explicitly.",
        }

    # ------------------------------------------------------------- helper --
    def _transition(self, state: str, reason: str, payload: dict | None = None) -> LiveSnapshot:
        current = self.current()
        if state == current.state:
            return current
        allowed = _AUTOMATIC.get(current.state, ())
        if state != BLOCKED and state not in allowed:
            raise LiveTransitionError(
                f"Illegal transition {current.state} -> {state} ({reason})")
        session_id = None
        ts = None
        if self.repository is not None:
            session_id = self.repository.record_live_session_transition(
                state=state, reason=reason,
                payload={**(payload or {}), "from_state": current.state})
            row = self.repository.current_live_session()
            ts = row.get("ts_entered") if row else None
        if self.bus is not None:
            self.bus.publish("live.state", {"state": state, "reason": reason,
                                            "from": current.state},
                             source="live-session", object_key="live")
        return LiveSnapshot(state, reason, ts)
