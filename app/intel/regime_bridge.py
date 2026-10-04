"""Bridge between the existing RegimeEngine and the AXIOM intelligence layer.

The regime engine (app.learning.regime_engine) already classifies regimes
from observable features and records REGIME_CHANGE learning events. This
bridge makes regime state a first-class "what changed" signal and feeds
regime transitions into the Opportunity Center — without duplicating the
classifier (spec sections 8, 12, 14).
"""

from __future__ import annotations


class RegimeBridge:
    def __init__(self, changes, opportunities, *, market_id: str = "india"):
        self.changes = changes
        self.opportunities = opportunities
        self.market_id = market_id
        self._last: dict[str, str] = {}

    def observe(self, identity: str, evaluation: dict) -> dict:
        """Consume one RegimeEngine.evaluate() result.

        Returns a summary of what the bridge did; every action is driven by
        the evaluation's facts, never invented.
        """
        label = evaluation.get("regime")
        if not label:
            return {"recorded": False, "reason": "evaluation carried no regime label"}
        previous = self._last.get(identity)
        self._last[identity] = label
        actions = {"recorded": True, "identity": identity, "regime": label,
                   "changed": False}
        if previous is None:
            # First observation is a snapshot, not a change (change-engine
            # semantics) — still visible as current state.
            self.changes.observe("regime", identity,
                                 {"label": label,
                                  "samples": evaluation.get("features", {}).get("samples")},
                                 evidence="regime engine initial snapshot")
            return actions
        if label != previous:
            actions["changed"] = True
            self.changes.observe("regime", identity, {"label": label},
                                 evidence=f"regime engine: {previous} -> {label}")
            self.opportunities.observe(
                market_id=self.market_id, identity=identity,
                regime_change=label,
                volatility_expansion="vol" in str(label).lower())
        return actions

    def evidence_value(self, identity: str) -> dict | None:
        label = self._last.get(identity)
        if label is None:
            return None
        return {"regime": label}
