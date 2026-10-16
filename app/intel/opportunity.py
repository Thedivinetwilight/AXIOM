"""Opportunity Center (master spec sections 14, 15, 139, 140).

A live research feed, NOT an auto-trading queue. Opportunities are detected
from observable feature deltas, carry an explicit lifecycle, and expire or
downgrade when their evidence goes stale.

Lifecycle: DETECTED -> QUALIFYING -> INVESTIGATING ->
           HIGH-CONVICTION SETUP | DEVELOPING | WATCH | AVOID | NO EDGE ->
           EXPIRED | INVALIDATED (terminal), plus EXECUTED/PAPERED/SHADOWED
           markers where applicable.

Attention mode (spec section 15) changes DETECTION thresholds and ranking
sensitivity only. It can never weaken risk, reconciliation, authentication,
emergency stop or live authorization — those systems never read this module's
thresholds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

DETECTED = "DETECTED"
QUALIFYING = "QUALIFYING"
INVESTIGATING = "INVESTIGATING"
HIGH_CONVICTION = "HIGH-CONVICTION SETUP"
DEVELOPING = "DEVELOPING"
WATCH = "WATCH"
EXPIRED = "EXPIRED"
INVALIDATED = "INVALIDATED"
AVOID = "AVOID"
NO_EDGE = "NO EDGE"
EXECUTED_MARKERS = ("EXECUTED", "PAPERED", "SHADOWED")

ACTIVE_STATES = (DETECTED, QUALIFYING, INVESTIGATING, HIGH_CONVICTION,
                 DEVELOPING, WATCH)

ATTENTION_PROFILES = {
    "conservative": {"move_threshold": 0.030, "volume_spike": 2.5,
                     "max_active": 5, "ttl_seconds": 600},
    "balanced": {"move_threshold": 0.020, "volume_spike": 2.0,
                 "max_active": 10, "ttl_seconds": 900},
    "aggressive": {"move_threshold": 0.012, "volume_spike": 1.5,
                   "max_active": 20, "ttl_seconds": 900},
}


@dataclass
class Opportunity:
    opportunity_id: str
    market_id: str
    identity: str
    state: str
    created_ts: float
    last_evidence_ts: float
    updated_ts: float
    trigger: str
    evidence: dict = field(default_factory=dict)
    invalidation_reason: str | None = None

    def age_s(self, now: float | None = None) -> float:
        return (now or time.time()) - self.created_ts

    def evidence_age_s(self, now: float | None = None) -> float:
        return (now or time.time()) - self.last_evidence_ts

    def to_dict(self, now: float | None = None) -> dict:
        now = now or time.time()
        return {
            "id": self.opportunity_id, "market_id": self.market_id,
            "identity": self.identity, "state": self.state,
            "age_seconds": round(self.age_s(now), 1),
            "evidence_age_seconds": round(self.evidence_age_s(now), 1),
            "trigger": self.trigger, "evidence": self.evidence,
            "invalidation_reason": self.invalidation_reason,
        }


class OpportunityCenter:
    """Detection + lifecycle + expiry. Persistence is delegated to the repo."""

    def __init__(self, repository=None, bus=None, *, attention_mode: str = "balanced",
                 clock=time.time):
        self.repository = repository
        self.bus = bus
        self.clock = clock
        self._items: dict[str, Opportunity] = {}
        self.set_attention_mode(attention_mode)

    # ---------------------------------------------------------- attention --
    def set_attention_mode(self, mode: str) -> dict:
        key = (mode or "balanced").lower()
        if key not in ATTENTION_PROFILES:
            raise ValueError(f"unknown attention mode '{mode}' "
                             f"({', '.join(ATTENTION_PROFILES)})")
        self.attention_mode = key
        self.profile = dict(ATTENTION_PROFILES[key])
        return {"attention_mode": self.attention_mode, "profile": self.profile,
                "note": "attention mode tunes detection/ranking only; it never "
                        "relaxes risk, reconciliation or live authorization"}

    # ----------------------------------------------------------- detection --
    def observe(self, *, market_id: str, identity: str,
                price_change_pct: float | None = None,
                volume_ratio: float | None = None,
                volatility_expansion: bool = False, regime_change: str | None = None,
                model_disagreement: bool = False, user_focus: bool = False,
                execution_eligible: bool = False) -> Opportunity | None:
        """Evaluate one instrument observation; create/refresh an opportunity.

        All inputs are OBSERVED facts supplied by the caller — the center
        never invents a trigger it was not given.
        """
        now = self.clock()
        triggers = []
        if price_change_pct is not None and \
                abs(price_change_pct) >= self.profile["move_threshold"]:
            triggers.append(f"price move {price_change_pct:+.2%}")
        if volume_ratio is not None and volume_ratio >= self.profile["volume_spike"]:
            triggers.append(f"volume spike x{volume_ratio:.1f}")
        if volatility_expansion:
            triggers.append("volatility expansion")
        if regime_change:
            triggers.append(f"regime change -> {regime_change}")
        if model_disagreement:
            triggers.append("model disagreement")
        if user_focus:
            triggers.append("user focus/pin")
        if not triggers:
            return None

        existing = self._active_for(market_id, identity)
        if existing:
            existing.last_evidence_ts = now
            existing.updated_ts = now
            existing.trigger = "; ".join(triggers)
            existing.evidence.update({
                "price_change_pct": price_change_pct, "volume_ratio": volume_ratio,
                "volatility_expansion": volatility_expansion,
                "regime_change": regime_change,
                "model_disagreement": model_disagreement,
                "execution_eligible": execution_eligible,
            })
            self._persist(existing)
            return existing

        if len(self._active_items()) >= self.profile["max_active"]:
            # bounded feed: drop the oldest low-state item rather than
            # growing without limit (spec section 96).
            self._evict_oldest()

        opportunity = Opportunity(
            opportunity_id=str(uuid4()), market_id=market_id, identity=identity,
            state=DETECTED, created_ts=now, last_evidence_ts=now, updated_ts=now,
            trigger="; ".join(triggers),
            evidence={"price_change_pct": price_change_pct,
                      "volume_ratio": volume_ratio,
                      "volatility_expansion": volatility_expansion,
                      "regime_change": regime_change,
                      "model_disagreement": model_disagreement,
                      "execution_eligible": execution_eligible})
        self._items[opportunity.opportunity_id] = opportunity
        self._persist(opportunity)
        self._publish("opportunity.detected", opportunity)
        return opportunity

    # ----------------------------------------------------------- lifecycle --
    def transition(self, opportunity_id: str, state: str, *,
                   reason: str = "", evidence: dict | None = None) -> Opportunity:
        opportunity = self._items.get(opportunity_id)
        if opportunity is None:
            raise KeyError(f"unknown opportunity {opportunity_id}")
        if opportunity.state in (EXPIRED, INVALIDATED):
            raise ValueError(f"opportunity {opportunity_id} is terminal "
                             f"({opportunity.state}); terminal states never revive")
        if state not in ACTIVE_STATES + (EXPIRED, INVALIDATED) + EXECUTED_MARKERS:
            raise ValueError(f"unknown opportunity state '{state}'")
        now = self.clock()
        opportunity.state = state
        opportunity.updated_ts = now
        if state in ACTIVE_STATES and evidence:
            opportunity.last_evidence_ts = now
            opportunity.evidence.update(evidence)
        if state == INVALIDATED:
            opportunity.invalidation_reason = reason or "invalidated"
        self._persist(opportunity)
        self._publish("opportunity.transition", opportunity)
        return opportunity

    def expire_stale(self) -> list[Opportunity]:
        """Expire opportunities whose evidence aged past the TTL (spec §139)."""
        now = self.clock()
        expired = []
        for opportunity in list(self._items.values()):
            if opportunity.state not in ACTIVE_STATES:
                continue
            if opportunity.evidence_age_s(now) > self.profile["ttl_seconds"]:
                opportunity.state = EXPIRED
                opportunity.updated_ts = now
                opportunity.invalidation_reason = (
                    f"evidence older than {self.profile['ttl_seconds']}s")
                self._persist(opportunity)
                self._publish("opportunity.expired", opportunity)
                expired.append(opportunity)
        return expired

    # -------------------------------------------------------------- queries --
    def list(self, *, market_id: str | None = None,
             state: str | None = None, now: float | None = None) -> list[dict]:
        now = now or self.clock()
        items = list(self._items.values())
        if market_id:
            items = [o for o in items if o.market_id == market_id]
        if state:
            items = [o for o in items if o.state == state]
        items.sort(key=lambda o: (o.state not in ACTIVE_STATES, -o.last_evidence_ts))
        return [o.to_dict(now=now) for o in items]

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for opportunity in self._items.values():
            out[opportunity.state] = out.get(opportunity.state, 0) + 1
        return out

    def get(self, opportunity_id: str) -> dict | None:
        opportunity = self._items.get(opportunity_id)
        return opportunity.to_dict(now=self.clock()) if opportunity else None

    # -------------------------------------------------------------- helpers --
    def _active_for(self, market_id: str, identity: str) -> Opportunity | None:
        for opportunity in self._items.values():
            if opportunity.market_id == market_id and opportunity.identity == identity \
                    and opportunity.state in ACTIVE_STATES:
                return opportunity
        return None

    def _active_items(self) -> list[Opportunity]:
        return [o for o in self._items.values() if o.state in ACTIVE_STATES]

    def _evict_oldest(self) -> None:
        active = sorted(self._active_items(), key=lambda o: o.last_evidence_ts)
        if not active:
            return
        victim = active[0]
        victim.state = EXPIRED
        victim.updated_ts = self.clock()
        victim.invalidation_reason = "evicted: attention capacity reached"
        self._persist(victim)
        self._publish("opportunity.expired", victim)

    def _persist(self, opportunity: Opportunity) -> None:
        if self.repository is None:
            return
        try:
            self.repository.upsert_opportunity({
                "id": opportunity.opportunity_id,
                "market_id": opportunity.market_id,
                "identity": opportunity.identity,
                "state": opportunity.state,
                "created_ts": _iso(opportunity.created_ts),
                "last_evidence_ts": _iso(opportunity.last_evidence_ts),
                "updated_ts": _iso(opportunity.updated_ts),
                "payload": {"trigger": opportunity.trigger,
                            "evidence": opportunity.evidence},
                "invalidation_reason": opportunity.invalidation_reason,
            })
        except Exception:
            # Persistence must never break detection; the in-memory state
            # remains authoritative for the live feed.
            pass

    def _publish(self, event_type: str, opportunity: Opportunity) -> None:
        if self.bus is not None:
            self.bus.publish(event_type, opportunity.to_dict(now=self.clock()),
                             source="opportunity-center",
                             object_key=opportunity.identity)


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
