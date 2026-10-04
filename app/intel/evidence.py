"""Evidence stack (master spec sections 16, 93, 94, 140, 141).

Never collapse the picture into one opaque AI score. Each layer of the stack
is an inspectable item with:

* ``kind``    — OBSERVED | CALCULATED | MODEL | HISTORICAL | HEURISTIC;
* ``status``  — present | STALE | UNAVAILABLE | UNCERTAIN;
* ``age_seconds`` — how old the component is (freshness honesty);
* ``provenance`` — exactly what produced it.

Recommendations carry a validity window; when material evidence changes they
are invalidated and the transition is recorded (spec section 140).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

KINDS = ("OBSERVED", "CALCULATED", "MODEL", "HISTORICAL", "HEURISTIC")
STATUSES = ("present", "STALE", "UNAVAILABLE", "UNCERTAIN")

# Recommendation vocabulary (spec section 3): evidence/state, not certainty.
RECOMMENDATIONS = ("HIGH-CONVICTION SETUP", "FAVORABLE", "DEVELOPING", "WATCH",
                   "AVOID", "NO EDGE")

LAYER_ORDER = ("market_state", "feature_state", "regime", "kronos_forecast",
               "historical_analogues", "strategy_evidence", "risk",
               "recommendation", "execution_eligibility")


@dataclass
class EvidenceItem:
    layer: str
    kind: str
    status: str
    value: dict
    provenance: str
    ts: float = field(default_factory=time.time)

    def age_s(self, now: float | None = None) -> float:
        return (now or time.time()) - self.ts

    def to_dict(self, now: float | None = None) -> dict:
        now = now or time.time()
        return {"layer": self.layer, "kind": self.kind, "status": self.status,
                "value": self.value, "provenance": self.provenance,
                "age_seconds": round(self.age_s(now), 1)}


class EvidenceStack:
    """One instrument's stack plus its recommendation lifecycle."""

    def __init__(self, *, identity: str, market_id: str, clock=time.time,
                 stale_after_s: float = 120.0):
        self.identity = identity
        self.market_id = market_id
        self.clock = clock
        self.stale_after_s = stale_after_s
        self._layers: dict[str, EvidenceItem] = {}
        self._recommendation: dict | None = None
        self._history: list[dict] = []

    def set(self, layer: str, *, kind: str, value: dict, provenance: str,
            status: str | None = None, unavailable_reason: str | None = None) -> EvidenceItem:
        if layer not in LAYER_ORDER:
            raise ValueError(f"unknown evidence layer '{layer}'")
        if kind not in KINDS:
            raise ValueError(f"unknown evidence kind '{kind}'")
        now = self.clock()
        if value is None or value == {}:
            item = EvidenceItem(layer=layer, kind=kind, status="UNAVAILABLE",
                                value={"unavailable_reason":
                                       unavailable_reason or "no data"},
                                provenance=provenance, ts=now)
        else:
            item = EvidenceItem(layer=layer, kind=kind,
                                status=status or "present", value=value,
                                provenance=provenance, ts=now)
        previous = self._layers.get(layer)
        self._layers[layer] = item
        if previous is not None and self._recommendation is not None:
            # Material evidence changed -> prior recommendation is stale.
            self._invalidate_recommendation(
                f"evidence layer '{layer}' changed ({previous.status} -> {item.status})")
        return item

    def recommend(self, state: str, *, rationale: str, validity_s: float = 600.0,
                  suggested_size: float | None = None,
                  max_permitted_size: float | None = None) -> dict:
        if state not in RECOMMENDATIONS:
            raise ValueError(f"unknown recommendation state '{state}'")
        now = self.clock()
        missing = [layer for layer in LAYER_ORDER[:7] if layer not in self._layers]
        recommendation = {
            "id": str(uuid4()),
            "identity": self.identity,
            "market_id": self.market_id,
            "state": state,
            "rationale": rationale,
            "ts": now,
            "valid_until": now + validity_s,
            "missing_layers": missing,
            "suggested_size": suggested_size,
            "max_permitted_size": max_permitted_size,
            "note": "recommendation is analysis, not an order; execution still "
                    "requires the full guard chain",
        }
        if self._recommendation is not None and self._recommendation["state"] != state:
            self._history.append({**self._recommendation,
                                  "invalidated_by": "superseded by new recommendation"})
        self._recommendation = recommendation
        return self.recommendation(now=now)

    def recommendation(self, now: float | None = None) -> dict | None:
        """Current recommendation or None when expired/invalidated (spec §140)."""
        if self._recommendation is None:
            return None
        now = now or self.clock()
        if now > self._recommendation["valid_until"]:
            self._invalidate_recommendation("validity window elapsed")
            return None
        if self._recommendation.get("invalidated"):
            return None
        out = dict(self._recommendation)
        out["age_seconds"] = round(now - out["ts"], 1)
        out["fresh"] = out["age_seconds"] <= self.stale_after_s
        return out

    def _invalidate_recommendation(self, reason: str) -> None:
        if self._recommendation is None or self._recommendation.get("invalidated"):
            return
        record = dict(self._recommendation)
        record["invalidated"] = True
        record["invalidation_reason"] = reason
        record["invalidated_ts"] = self.clock()
        self._history.append(record)
        self._recommendation = None

    def to_dict(self, now: float | None = None) -> dict:
        now = now or self.clock()
        layers = []
        for layer in LAYER_ORDER:
            item = self._layers.get(layer)
            if item is None:
                layers.append({"layer": layer, "status": "NOT LOADED"})
                continue
            entry = item.to_dict(now=now)
            if entry["status"] == "present" and item.age_s(now) > self.stale_after_s:
                entry["status"] = "STALE"
            layers.append(entry)
        return {"identity": self.identity, "market_id": self.market_id,
                "layers": layers,
                "recommendation": self.recommendation(now=now),
                "recommendation_history_size": len(self._history)}
