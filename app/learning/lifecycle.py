"""Strategy lifecycle: promotion pipeline + degradation ladder.

Promotion stages (one step at a time, evidence recorded, never skipped):
  RESEARCH → VALIDATION → WALK_FORWARD → SHADOW → CANARY → APPROVED
Only APPROVED + ACTIVE strategies are tradable. Degradation (recorded,
evidence-based): ACTIVE → PROBATION → SHADOW → RETRIED... RETIRED stays in
history forever (append-only; children never trade on parent success).
"""

from __future__ import annotations

import uuid

STAGES = ("RESEARCH", "VALIDATION", "WALK_FORWARD", "SHADOW", "CANARY",
          "APPROVED")
HEALTH = ("ACTIVE", "PROBATION", "SHADOW", "RETIRED")
DEGRADATION_STEPS = ("PROBATION", "SHADOW", "RETIRED")
TRADABLE_STATUS = {"ACTIVE"}


def lineage_id_for(strategy: str, parameters: dict, generation: int,
                   parent_id: str | None) -> str:
    """Deterministic id: same strategy+params+lineage ⇒ same lineage row."""
    key = f"lineage|{strategy}|{sorted(parameters.items())}|{generation}|{parent_id}"
    return uuid.uuid5(uuid.NAMESPACE_URL, key).hex


class StrategyLifecycleManager:
    def __init__(self, repository):
        self.repository = repository

    def _current(self, lineage_id):
        return self.repository.current_lifecycle(lineage_id)

    def promote(self, lineage_id: str, *, evidence: dict | None = None) -> dict:
        current = self._current(lineage_id)
        if current is None:
            return {"promoted": False, "reason": "unknown lineage id"}
        stage = current["stage"]
        if stage not in STAGES:
            return {"promoted": False, "reason": f"unknown stage {stage}"}
        if current["status"] != "ACTIVE":
            return {"promoted": False,
                    "reason": f"strategy is {current['status']}; recover first"}
        if stage == "APPROVED":
            return {"promoted": False, "reason": "already APPROVED"}
        next_stage = STAGES[STAGES.index(stage) + 1]
        self.repository.record_lifecycle(lineage_id, next_stage, "ACTIVE",
                                         evidence or {"why": "promotion step"})
        return {"promoted": True, "stage": next_stage}

    def is_tradable(self, lineage_id: str) -> bool:
        current = self._current(lineage_id)
        return bool(current and current["stage"] == "APPROVED"
                    and current["status"] in TRADABLE_STATUS)

    def detect_degradation(self, lineage_id: str, recent_returns: list[float],
                           *, baseline_expectancy: float) -> dict:
        """Evidence-based health step. Consecutive degraded checks move the
        strategy down the ladder; a healthy check recovers to ACTIVE."""
        current = self._current(lineage_id) or {"stage": "RESEARCH",
                                                "status": "ACTIVE"}
        recent = float(sum(recent_returns) / len(recent_returns)) \
            if recent_returns else 0.0
        degraded = recent <= 0 or recent < 0.5 * float(baseline_expectancy)
        evidence = {"recent_expectancy": recent,
                    "baseline_expectancy": float(baseline_expectancy),
                    "samples": len(recent_returns)}
        if degraded:
            status = current["status"]
            if status == "RETIRED":
                new_status = "RETIRED"  # history kept; never resurrected silently
            else:
                index = DEGRADATION_STEPS.index(status) + 1 \
                    if status in DEGRADATION_STEPS else 0
                new_status = DEGRADATION_STEPS[min(index, len(DEGRADATION_STEPS) - 1)]
        else:
            new_status = "ACTIVE"
        self.repository.record_lifecycle(lineage_id, current["stage"],
                                         new_status, evidence)
        return {"status": new_status, "degraded": degraded,
                "evidence": evidence}
