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

    def record_verified(self, *, when: datetime | None = None, source: str = "") -> dict:
        """Mark the credential as verified-NOW (call after a successful probe)."""
        now = when or self._now()
        state = {"verified_at": now.isoformat(), "source": source}
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

    def check(self, *, now: datetime | None = None) -> GuardDecision:
        """Entries allowed only when the credential is verified and fresh."""
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
        deadline = verified + timedelta(hours=self.max_age_hours)
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
        return self.repository.get_engine_state(CANARY_KEY, {}) or {}

    def submitted(self) -> int:
        try:
            return int(self._load().get("submitted", 0))
        except (TypeError, ValueError):
            return 0

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
        if not self.enabled:
            return GuardDecision(True, "CANARY_DISABLED")
        count = self.submitted()
        if count >= self.first_orders:
            return GuardDecision(True, "CANARY_COMPLETE")
        if price is None or price <= 0 or quantity is None or quantity <= 0:
            return GuardDecision(
                False, "CANARY_INVALID_INPUT: entry needs a positive price and quantity")
        notional = float(quantity) * float(price)
        if notional <= self.max_notional:
            return GuardDecision(
                True, f"CANARY_OK ({count + 1}/{self.first_orders}, "
                      f"notional {notional:,.2f} ≤ {self.max_notional:,.2f})")
        allowed_qty = int(self.max_notional // float(price))
        if allowed_qty <= 0:
            return GuardDecision(
                False, f"CANARY_BLOCKED: one unit costs {price:,.2f} but the "
                       f"canary ceiling is {self.max_notional:,.2f}")
        return GuardDecision(
            True,
            f"CANARY_REDUCED ({count + 1}/{self.first_orders}): quantity "
            f"{quantity} → {allowed_qty} to respect the "
            f"{self.max_notional:,.2f} canary ceiling",
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
