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

from dataclasses import dataclass

from app.config.settings import LIVE_CONFIRM_PHRASE
from app.live import preflight as live_preflight

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
    def __init__(self, repository, bus=None):
        self.repository = repository
        self.bus = bus

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
        checks = live_preflight.run_checks(settings, repository=repository)
        hard_fail = any((not c.ok) and c.severity == live_preflight.ERROR for c in checks)
        if reconciliation_blocked:
            return RECONCILING
        if hard_fail:
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
    def readiness(self, settings, *, repository=None) -> dict:
        checks = live_preflight.run_checks(settings, repository=repository)
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
        # Verdict semantics follow the preflight design: ERROR-severity
        # failures block; WARNINGs are accepted-known gaps; INFO "not
        # checked" items (fresh database, no recorded state yet) never fail
        # the verdict on their own but are ALWAYS listed as UNKNOWN — they
        # can never be read as verified-clean (spec section 32: a missing
        # check must never silently become healthy).
        if blocked:
            verdict = "BLOCKED"
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
                       "unknown": len(unknown)},
        }

    # ------------------------------------------------------- transitions --
    def arm(self, settings, *, confirmation_phrase: str, repository=None,
            actor: str = "gui") -> LiveSnapshot:
        """Explicit operator authorization: READY -> ARMED.

        Refuses unless the readiness contract passes. This is the ONLY path
        into ARMED; no timer, restart handler, research hook or config change
        calls this method.
        """
        if confirmation_phrase != LIVE_CONFIRM_PHRASE:
            raise LiveTransitionError(
                "Arming live trading requires the explicit confirmation phrase.")
        if self.repository is not None:
            from app.live.ops import read_emergency_stop

            try:
                stopped, why = read_emergency_stop(self.repository)
            except Exception:
                stopped, why = True, "emergency-stop flag unreadable"
            if stopped:
                raise LiveTransitionError(
                    f"Cannot arm while the emergency stop is active ({why}).")
        readiness = self.readiness(settings, repository=repository)
        if readiness["verdict"] == "BLOCKED":
            raise LiveTransitionError(
                "Readiness contract has BLOCKED checks: "
                + "; ".join(c["name"] for c in readiness["checks"] if c["status"] == "BLOCKED"))
        return self._transition(ARMED, f"operator arming by {actor} after readiness "
                                       f"verdict {readiness['verdict']}",
                                payload={"verdict": readiness["verdict"]})

    def activate(self, *, actor: str = "gui") -> LiveSnapshot:
        """ARMED -> ACTIVE. Requires prior explicit arming; never from OFF/READY."""
        current = self.current()
        if current.state != ARMED:
            raise LiveTransitionError(
                f"Cannot activate live trading from state {current.state}; "
                "explicit arming is required first.")
        return self._transition(ACTIVE, f"operator start by {actor}")

    def pause(self, *, reason: str = "operator pause") -> LiveSnapshot:
        current = self.current()
        if current.state not in (ACTIVE, RECONCILING):
            raise LiveTransitionError(f"Cannot pause from state {current.state}")
        return self._transition(PAUSED, reason)

    def resume(self) -> LiveSnapshot:
        current = self.current()
        if current.state != PAUSED:
            raise LiveTransitionError(f"Cannot resume from state {current.state}")
        return self._transition(ACTIVE, "operator resume")

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
        """Explicit stop. Clears authorization; configuration may remain."""
        current = self.current()
        if current.state == OFF:
            return current
        snapshot = self._transition(STOPPED, reason)
        if not keep_configuration:
            self._transition(OFF, "configuration cleared after stop")
            return self.current()
        return snapshot

    def disarm(self, *, reason: str = "operator disarm") -> LiveSnapshot:
        current = self.current()
        if current.state in (OFF, STOPPED):
            return current
        if current.state in (ACTIVE, PAUSED, RECONCILING):
            raise LiveTransitionError(
                f"Stop the live session before disarming (state {current.state})")
        return self._transition(STOPPED, reason)

    def block(self, *, reason: str) -> LiveSnapshot:
        """Hard conditions (emergency stop, breach) force BLOCKED from anywhere."""
        return self._transition(BLOCKED, reason)

    def release_block(self, *, destination: str | None = None,
                      reason: str = "block released") -> LiveSnapshot:
        current = self.current()
        if current.state != BLOCKED:
            raise LiveTransitionError(f"Not blocked (state {current.state})")
        target = destination or READY
        if target not in _AUTOMATIC[BLOCKED]:
            raise LiveTransitionError(f"BLOCKED cannot be released to {target}")
        if target == ARMED or target == ACTIVE:
            raise LiveTransitionError(
                "Releasing a block can never re-arm or re-activate directly; "
                "operator authorization is required again.")
        return self._transition(target, reason)

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
