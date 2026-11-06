"""Broker LIVE gate: readiness checks, explicit Arm + Activate, kill switch.

LIVE trading is disabled by default and stays unavailable until every
readiness check passes AND the operator performs two distinct explicit
actions: Arm, then Activate. Only activation mints the
:class:`~app.alpaca_broker.client.LivePermit` that the LIVE client requires.

Every state change is journaled; any material change (credentials, risk
limits, account identity, reconciliation failure, stale data) disarms and
invalidates prior activation. The kill switch blocks ALL order actions until
an explicit operator reset, after which the gate returns to DISABLED and must
be armed and activated again. There is no implicit path to LIVE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from app.alpaca_broker.client import LivePermit


class GateState(str, Enum):
    DISABLED = "DISABLED"          # default; LIVE unavailable
    ARMED = "ARMED"                # readiness verified; not yet active
    ACTIVE = "ACTIVE"              # permit issued; LIVE submissions allowed
    KILL_SWITCH = "KILL_SWITCH"    # emergency stop engaged; everything blocked


class GateError(RuntimeError):
    """Fail-closed gate refusal. The message states exactly what is missing."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BrokerLiveGate:
    """State machine for the Alpaca LIVE path. Paper paths do not consult it."""

    state: GateState = GateState.DISABLED
    kill_engaged: bool = False
    kill_reason: str | None = None
    armed_at: str | None = None
    activated_at: str | None = None
    last_readiness: dict = field(default_factory=dict)
    _permit: LivePermit | None = field(default=None, repr=False)

    # -- readiness -------------------------------------------------------------

    def readiness_snapshot(self, checks: dict) -> dict:
        """Record and return the readiness report. All checks must be True."""
        if not isinstance(checks, dict) or not checks:
            raise GateError("readiness checks are missing; LIVE is unavailable")
        self.last_readiness = {
            "checked_at": _now(),
            "checks": {str(k): bool(v) for k, v in checks.items()},
            "all_passed": all(bool(v) for v in checks.values()),
        }
        return dict(self.last_readiness)

    def _require_all_passed(self) -> None:
        snapshot = self.last_readiness or {}
        checks = snapshot.get("checks") or {}
        if not checks:
            raise GateError("no readiness checks recorded; run readiness before arming")
        failed = sorted(name for name, ok in checks.items() if not ok)
        if failed:
            raise GateError(f"readiness checks failed: {', '.join(failed)}; LIVE is unavailable")

    # -- Arm / Activate ---------------------------------------------------------

    def arm(self, checks: dict) -> dict:
        if self.kill_engaged:
            raise GateError("kill switch engaged; reset it before any LIVE action")
        self.readiness_snapshot(checks)
        self._require_all_passed()
        self.state = GateState.ARMED
        self.armed_at = _now()
        self.activated_at = None
        self._permit = None
        return self.status()

    def activate(self) -> LivePermit:
        if self.kill_engaged:
            raise GateError("kill switch engaged; reset it before any LIVE action")
        if self.state != GateState.ARMED:
            raise GateError("LIVE requires an explicit Arm before Activate")
        self._require_all_passed()
        self.state = GateState.ACTIVE
        self.activated_at = _now()
        self._permit = LivePermit(
            gate_id=f"alpaca-live-{self.armed_at}-{self.activated_at}",
            issued_at=self.activated_at,
        )
        return self._permit

    def permit(self) -> LivePermit | None:
        return self._permit if self.state == GateState.ACTIVE else None

    def disarm(self, reason: str = "operator disarm") -> dict:
        self.state = GateState.KILL_SWITCH if self.kill_engaged else GateState.DISABLED
        self.armed_at = None
        self.activated_at = None
        self._permit = None
        self.last_disarm_reason = reason
        return self.status()

    # -- kill switch ------------------------------------------------------------

    def engage_kill_switch(self, reason: str) -> dict:
        self.kill_engaged = True
        self.kill_reason = reason
        return self.disarm(f"kill switch: {reason}")

    def reset_kill_switch(self, *, operator_ack: bool = False) -> dict:
        if not operator_ack:
            raise GateError("kill-switch reset requires an explicit operator acknowledgement")
        self.kill_engaged = False
        self.kill_reason = None
        self.state = GateState.DISABLED
        return self.status()

    # -- visibility --------------------------------------------------------------

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "kill_engaged": self.kill_engaged,
            "kill_reason": self.kill_reason,
            "armed_at": self.armed_at,
            "activated_at": self.activated_at,
            "permit_active": self._permit is not None and self.state == GateState.ACTIVE,
            "readiness": dict(self.last_readiness),
            "live_available": self.state == GateState.ACTIVE and not self.kill_engaged,
            "disarm_reason": getattr(self, "last_disarm_reason", None),
        }
