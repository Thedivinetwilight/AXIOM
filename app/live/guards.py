"""Pre-entry guards for real-money trading (V2.1 L-ops).

Both guards gate **new entries only**. Protective exits and risk-reducing
actions must never be blocked by an operational rail — a guard that traps the
account in a losing position is worse than the risk it prevents — so the engine
calls these only for entry submissions.

* :class:`TokenGuard` — refuses entries when the credential has not been
  verified recently. Upstox access tokens expire daily; a stale token means the
  broker will reject orders (or, worse, a partially working session), so the
  engine stops opening new positions before that point instead of discovering it
  mid-order.
* :class:`CanaryGuard` — caps the notional of the first ``first_orders`` live
  entries. A fat-fingered quantity or a unit bug in sizing therefore risks a
  tiny amount of real money first, and the ramp ends by itself after
  ``first_orders`` entries (no operator action needed to "go live fully").

State is persisted in ``engine_state`` so a restart cannot bypass a ramp or
reset a freshness clock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: engine_state keys (namespaced so they never collide with engine keys)
TOKEN_KEY = "live_token_verified"
CANARY_KEY = "live_canary_state"


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str
    adjusted_quantity: float | None = None

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "adjusted_quantity": self.adjusted_quantity}


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class TokenGuard:
    """Refuse new entries when the trading credential is too old to trust."""

    def __init__(self, repository, *, max_age_hours: float = 20.0,
                 warn_minutes: int = 120, now_fn=None):
        self.repository = repository
        self.max_age_hours = float(max_age_hours)
        self.warn_minutes = int(warn_minutes)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # -- write side ----------------------------------------------------------

    def record_verified(self, *, when: datetime | None = None, source: str = "",
                         credential: str | None = None) -> dict:
        """Record a successful probe and bind it to that exact credential.

        Only a SHA-256 fingerprint is persisted; the bearer token never enters
        engine_state, events, logs, or report output. Callers that do not have
        a real credential (for example, offline drills) may omit it, but such
        a record cannot authorize a live entry.
        """
        now = when or self._now()
        state = {"verified_at": now.isoformat(), "source": source}
        if credential is not None:
            if not isinstance(credential, str) or not credential.strip():
                raise ValueError("verified credential must be a non-empty string")
            state["credential_sha256"] = hashlib.sha256(
                credential.encode("utf-8")).hexdigest()
        if self.repository is not None:
            self.repository.set_engine_state(TOKEN_KEY, state)
        return state

    def verified_at(self) -> datetime | None:
        if self.repository is None:
            return None
        raw = self.repository.get_engine_state(TOKEN_KEY, {}) or {}
        return _parse(raw.get("verified_at", ""))

    # -- read side -----------------------------------------------------------

    def age_hours(self, *, now: datetime | None = None) -> float | None:
        verified = self.verified_at()
        if verified is None:
            return None
        now = now or self._now()
        return (now - verified).total_seconds() / 3600.0

    def check(self, *, now: datetime | None = None,
              credential: str | None = None) -> GuardDecision:
        """Allow only a fresh, successfully probed credential.

        Supplying ``credential`` enables exact credential binding for the live
        gateway. Historical timestamp-only records remain useful for advisory
        status, but are not proof that the currently configured token passed
        the probe.
        """
        if self.max_age_hours <= 0:
            return GuardDecision(True, "TOKEN_AGE_CHECK_DISABLED")
        now = now or self._now()
        verified = self.verified_at()
        if verified is None:
            # Fail closed: an unverified credential in a real-money session is
            # exactly the state we must not open positions from.
            return GuardDecision(
                False, "TOKEN_UNVERIFIED: run 'python -m app live-preflight' "
                       "to verify the live credential before trading")
        if credential is not None:
            if not isinstance(credential, str) or not credential.strip():
                return GuardDecision(False, "TOKEN_CREDENTIAL_UNBOUND: no credential supplied")
            try:
                state = self.repository.get_engine_state(TOKEN_KEY, {}) or {}
                stored_hash = (state.get("credential_sha256")
                               if isinstance(state, dict) else None)
            except Exception:
                return GuardDecision(False, "TOKEN_CREDENTIAL_PROVENANCE_UNKNOWN")
            expected_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()
            if (not isinstance(stored_hash, str) or len(stored_hash) != 64
                    or not hmac.compare_digest(stored_hash, expected_hash)):
                return GuardDecision(
                    False, "TOKEN_CREDENTIAL_CHANGED_OR_UNBOUND: rerun live-preflight")
        try:
            age_seconds = (now - verified).total_seconds()
            max_age = float(self.max_age_hours)
        except (TypeError, ValueError, OverflowError):
            return GuardDecision(False, "TOKEN_FRESHNESS_UNKNOWN: invalid clock or bound")
        if (not math.isfinite(age_seconds) or not math.isfinite(max_age)
                or max_age < 0):
            return GuardDecision(False, "TOKEN_FRESHNESS_UNKNOWN: invalid clock or bound")
        if age_seconds < -60.0:
            return GuardDecision(
                False, "TOKEN_VERIFICATION_FUTURE_DATED: clock/provenance is unknown")
        deadline = verified + timedelta(hours=max_age)
        if now >= deadline:
            return GuardDecision(
                False, f"TOKEN_STALE: last verified {self.age_hours(now=now):.1f}h ago "
                       f"(limit {self.max_age_hours:g}h); refresh the token and re-run "
                       f"live-preflight")
        return GuardDecision(True, "TOKEN_FRESH")

    def expires_in_minutes(self, *, now: datetime | None = None) -> float | None:
        if self.max_age_hours <= 0:
            return None
        verified = self.verified_at()
        if verified is None:
            return None
        now = now or self._now()
        deadline = verified + timedelta(hours=self.max_age_hours)
        return (deadline - now).total_seconds() / 60.0

    def warn_due(self, *, now: datetime | None = None) -> bool:
        """True when the credential is inside the pre-expiry warning window."""
        if self.warn_minutes <= 0:
            return False
        remaining = self.expires_in_minutes(now=now)
        return remaining is not None and 0 <= remaining <= self.warn_minutes

    def status(self, *, now: datetime | None = None) -> dict:
        verified = self.verified_at()
        return {
            "verified_at": verified.isoformat() if verified else None,
            "age_hours": None if self.age_hours(now=now) is None
            else round(self.age_hours(now=now), 3),
            "expires_in_minutes": None if self.expires_in_minutes(now=now) is None
            else round(self.expires_in_minutes(now=now), 1),
            "max_age_hours": self.max_age_hours,
            "decision": self.check(now=now).to_dict(),
        }


class CanaryGuard:
    """Cap the notional of the first live entries, then step out of the way."""

    def __init__(self, repository, *, max_notional: float = 0.0,
                 first_orders: int = 10, now_fn=None):
        self.repository = repository
        self.max_notional = float(max_notional)
        self.first_orders = int(first_orders)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    @property
    def enabled(self) -> bool:
        return self.max_notional > 0 and self.first_orders > 0

    # -- state ---------------------------------------------------------------

    def _load(self) -> dict:
        if self.repository is None:
            return {}
        state = self.repository.get_engine_state(CANARY_KEY, {})
        if state is None:
            return {}
        if not isinstance(state, dict):
            raise ValueError("live canary state is malformed")
        return state

    def submitted(self) -> int:
        value = self._load().get("submitted", 0)
        if type(value) is not int or value < 0:
            raise ValueError("live canary submitted count is malformed")
        return value

    def _persist(self, submitted: int) -> None:
        if self.repository is None:
            return
        self.repository.set_engine_state(CANARY_KEY, {
            "submitted": int(submitted),
            "max_notional": self.max_notional,
            "first_orders": self.first_orders,
            "updated_at": self._now().isoformat(),
        })

    def record_submitted(self, *, quantity: float = 0.0, price: float = 0.0,
                         notional: float | None = None) -> int:
        """Count one ramped entry. Returns the new submitted count."""
        count = self.submitted() + 1
        self._persist(count)
        return count

    def reserve_submission(self, *, intent_id: str, quantity: float,
                           price: float) -> GuardDecision:
        """Durably reserve the final canary count before a broker call.

        The reservation is monotonic: even a lost broker response consumes one
        canary attempt, rather than allowing a retry or concurrent submission
        to exceed the configured ramp. The execution journal separately
        prevents resubmission of the same intent.
        """
        if not isinstance(intent_id, str) or not intent_id.strip():
            return GuardDecision(False, "CANARY_INTENT_ID_MISSING")
        try:
            qty = float(quantity)
            px = 0.0 if price is None else float(price)
            maximum = float(self.max_notional)
        except (TypeError, ValueError, OverflowError):
            return GuardDecision(False, "CANARY_INVALID_INPUT")
        if (not math.isfinite(qty) or qty <= 0 or not math.isfinite(px)
                or px < 0 or not math.isfinite(maximum) or maximum < 0
                or type(self.first_orders) is not int or self.first_orders < 0):
            return GuardDecision(False, "CANARY_INVALID_INPUT")
        if self.repository is None or getattr(self.repository, "db", None) is None:
            return GuardDecision(False, "CANARY_STATE_UNAVAILABLE")
        try:
            with self.repository.db.transaction() as db:
                # Serialize count/check/write across threads and processes.
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM engine_state WHERE key=?", (CANARY_KEY,)
                ).fetchone()
                if row is None:
                    state = {}
                else:
                    state = json.loads(row[0])
                    if not isinstance(state, dict):
                        raise ValueError("canary state is not an object")
                count = state.get("submitted", 0)
                if type(count) is not int or count < 0:
                    raise ValueError("canary submitted count is malformed")
                recorded_max = state.get("max_notional")
                if recorded_max is not None:
                    if (isinstance(recorded_max, bool)
                            or not isinstance(recorded_max, (int, float))
                            or not math.isfinite(float(recorded_max))
                            or not math.isclose(float(recorded_max), maximum,
                                                rel_tol=0.0, abs_tol=1e-9)):
                        raise ValueError("canary configuration changed mid-session")
                recorded_orders = state.get("first_orders")
                if recorded_orders is not None and (
                        type(recorded_orders) is not int
                        or recorded_orders != self.first_orders):
                    raise ValueError("canary configuration changed mid-session")
                reservations = state.get("reserved_intents", [])
                if (not isinstance(reservations, list)
                        or any(not isinstance(value, str) for value in reservations)):
                    raise ValueError("canary intent reservations are malformed")
                if intent_id in reservations:
                    return GuardDecision(False, "CANARY_DUPLICATE_RESERVATION")
                if self.enabled and count < self.first_orders:
                    notional = qty * px
                    if px <= 0 or not math.isfinite(notional):
                        return GuardDecision(
                            False, "CANARY_INVALID_INPUT: finite positive price required")
                    if notional > maximum:
                        return GuardDecision(
                            False, "CANARY_LIMIT: final intent exceeds remaining ramp cap")
                reservations = (reservations + [intent_id])[-10000:]
                state.update({
                    "submitted": count + 1,
                    "max_notional": maximum,
                    "first_orders": self.first_orders,
                    "updated_at": self._now().isoformat(),
                    "reserved_intents": reservations,
                })
                payload = json.dumps(state, allow_nan=False, separators=(",", ":"))
                db.execute(
                    "INSERT INTO engine_state(key,payload) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                    (CANARY_KEY, payload))
            return GuardDecision(True, "CANARY_SUBMISSION_RESERVED")
        except Exception as exc:
            return GuardDecision(
                False, f"CANARY_STATE_UNKNOWN ({type(exc).__name__})")

    def record_fill(self) -> int:
        """Optional: record a completed canary fill (informational)."""
        state = self._load()
        filled = int(state.get("filled", 0)) + 1
        state.update(filled=filled, updated_at=self._now().isoformat())
        if self.repository is not None:
            self.repository.set_engine_state(CANARY_KEY, state)
        return filled

    # -- decision ------------------------------------------------------------

    def check(self, *, quantity: float, price: float) -> GuardDecision:
        """Allow, shrink or refuse ONE entry against the canary ceiling."""
        try:
            qty = float(quantity)
            px = float(price)
            maximum = float(self.max_notional)
        except (TypeError, ValueError, OverflowError):
            return GuardDecision(
                False, "CANARY_INVALID_INPUT: entry needs numeric price and quantity")
        if (not math.isfinite(qty) or qty <= 0 or not math.isfinite(px)
                or px <= 0 or not math.isfinite(maximum) or maximum < 0):
            return GuardDecision(
                False, "CANARY_INVALID_INPUT: entry price/quantity/limit is invalid")
        if not self.enabled:
            return GuardDecision(True, "CANARY_DISABLED")
        count = self.submitted()
        if count >= self.first_orders:
            return GuardDecision(True, "CANARY_COMPLETE")
        notional = qty * px
        if not math.isfinite(notional):
            return GuardDecision(False, "CANARY_INVALID_INPUT: notional overflow")
        if notional <= maximum:
            return GuardDecision(
                True, f"CANARY_OK ({count + 1}/{self.first_orders}, "
                      f"notional {notional:,.2f} ≤ {maximum:,.2f})")
        allowed_qty = int(maximum // px)
        if allowed_qty <= 0:
            return GuardDecision(
                False, f"CANARY_BLOCKED: one unit costs {px:,.2f} but the "
                       f"canary ceiling is {maximum:,.2f}")
        return GuardDecision(
            True,
            f"CANARY_REDUCED ({count + 1}/{self.first_orders}): quantity "
            f"{quantity} → {allowed_qty} to respect the "
            f"{maximum:,.2f} canary ceiling",
            adjusted_quantity=float(allowed_qty))

    def status(self) -> dict:
        submitted = self.submitted()
        return {
            "enabled": self.enabled,
            "max_notional": self.max_notional,
            "first_orders": self.first_orders,
            "submitted": submitted,
            "remaining": max(self.first_orders - submitted, 0) if self.enabled else 0,
            "complete": bool(self.enabled and submitted >= self.first_orders),
        }
