"""Adaptive strategy selection: router-driven execution source.

Bridges research (discovery/lifecycle/router) to execution: the sandbox
engine consults this source for the ACTIVE strategy version. Selection uses
only candidates with positive out-of-sample evidence (StrategyRouter rejects
negative/insufficient OOS by construction), discounts degraded strategies
(lifecycle PROBATION/SHADOW/RETIRED), applies confidence calibration AND —
3.0.2 — requires the COMPLETE lifecycle ladder at the final execution
boundary: RESEARCH → VALIDATION → WALK_FORWARD → APPROVED with current
status ACTIVE. ``promoted=True`` alone is NOT tradable. When no candidate
qualifies, the recorded decision is explicitly NON-executable (diagnostics
only): the engine must not enter on any fallback strategy.
Every decision is persisted (ROUTER_DECISION learning event + engine_state
`active_strategy`) so the executed strategy version is always reconstructible.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.learning.lifecycle import StrategyLifecycleManager, lineage_id_for
from app.learning.router import StrategyRouter
from app.strategies import create_strategy

# the complete required research ladder BEFORE anything may trade (3.0.2)
REQUIRED_LADDER = ("RESEARCH", "VALIDATION", "WALK_FORWARD", "APPROVED")


def _load(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value or {}


class RouterStrategySource:
    """Selects the active strategy from recorded, promoted candidates.

    3.0.2: a candidate is TRADABLE only when its full research ladder is
    recorded (RESEARCH → VALIDATION → WALK_FORWARD → APPROVED, current status
    ACTIVE). promoted=True alone never reaches the execution boundary.
    """

    def __init__(self, repository, *, calibration=None,
                 fallback=("momentum", {"period": 2, "threshold": 0.0}),
                 router: StrategyRouter | None = None,
                 lifecycle: StrategyLifecycleManager | None = None):
        self.repository = repository
        self.calibration = calibration
        # diagnostics ONLY: never instantiated for execution (see select)
        self.fallback = fallback
        self.router = router or StrategyRouter()
        self.lifecycle = lifecycle or StrategyLifecycleManager(repository)

    def lineage_for(self, strategy: str, parameters: dict) -> str:
        """Documented root lineage id for a (strategy, parameters) version."""
        return lineage_id_for(strategy, parameters, 0, None)

    def lifecycle_eligibility(self, strategy: str, parameters: dict) -> dict:
        """Complete-lifecycle eligibility check (final execution boundary).

        Eligible ⇔ the lineage history contains EVERY required ladder stage
        (RESEARCH, VALIDATION, WALK_FORWARD, APPROVED) AND the current row is
        APPROVED + ACTIVE. Anything less (promoted-only, partially approved,
        inactive, degraded, retired, unrecorded) is INELIGIBLE.
        """
        lineage = self.lineage_for(strategy, parameters)
        history = self.repository.lifecycle_history(lineage)
        if not history:
            return {"eligible": False, "lineage": lineage,
                    "reason": "no lifecycle record (promoted alone is not tradable)"}
        stages = {row["stage"] for row in history}
        missing = [stage for stage in REQUIRED_LADDER if stage not in stages]
        if missing:
            return {"eligible": False, "lineage": lineage,
                    "reason": f"incomplete research ladder; missing {missing}"}
        current = history[-1]
        if current["stage"] != "APPROVED":
            return {"eligible": False, "lineage": lineage,
                    "reason": f"current stage {current['stage']} is not APPROVED"}
        if current["status"] not in ("ACTIVE",):
            return {"eligible": False, "lineage": lineage,
                    "reason": f"current status {current['status']} is not ACTIVE"}
        return {"eligible": True, "lineage": lineage, "reason": "APPROVED+ACTIVE"}

    def _candidates(self) -> list[dict]:
        candidates = []
        for row in self.repository.list_candidates(promoted=True, limit=100):
            oos = _load(row.get("oos"))
            robustness = _load(row.get("robustness"))
            candidates.append({
                "strategy": row["strategy"],
                "parameters": _load(row.get("parameters")),
                "oos": {"oos_expectancy": float(oos.get("oos_expectancy") or 0.0),
                        "oos_trades": int(oos.get("oos_trades") or 0)},
                "metrics": {"max_drawdown": float((_load(row.get("metrics"))
                                                  ).get("max_drawdown") or 0.0)},
                "robustness_score": float(robustness.get("score") or 0.0),
                "candidate_id": row["id"],
            })
        return candidates

    def _degradation(self) -> dict[str, float]:
        degradation: dict[str, float] = {}
        for lineage in self.repository.list_lineage(limit=500):
            current = self.repository.current_lifecycle(lineage["id"])
            if current and current["status"] in {"PROBATION", "SHADOW", "RETIRED"}:
                # severity: PROBATION −1 (score halved), SHADOW/RETIRED −3
                # (hard exclusion in the router)
                degradation[lineage["strategy"]] = \
                    -1.0 if current["status"] == "PROBATION" else -3.0
        return degradation

    def select(self, regime: str | None) -> tuple[dict | None, object | None]:
        """Returns (decision_record, strategy_instance_or_None).

        decision_record is ALWAYS persisted (selected or fallback) so every
        strategy change is versioned and observable.
        """
        candidates = self._candidates()
        degradation = self._degradation()
        calibration_discount = {}
        if self.calibration is not None:
            calibration_discount = {
                candidate["strategy"]: self.calibration.discount(candidate["strategy"])
                for candidate in candidates}
        decision = self.router.select(regime, candidates,
                                      degradation=degradation,
                                      calibration_discount=calibration_discount)
        decision["ts"] = datetime.now(timezone.utc).isoformat()
        decision["regime"] = regime
        strategy_instance = None
        if decision.get("selected"):
            match = next((c for c in candidates
                          if c["strategy"] == decision["strategy"]
                          and c["parameters"] == decision.get("parameters")), None)
            decision["candidate_id"] = match["candidate_id"] if match else None
            # 3.0.2 FINAL EXECUTION BOUNDARY: full lifecycle eligibility is
            # mandatory even when the router scored the candidate highest.
            eligibility = self.lifecycle_eligibility(
                decision["strategy"], decision.get("parameters") or {})
            decision["lifecycle"] = eligibility
            if not eligibility["eligible"]:
                decision["selected"] = False
                decision["reason"] = ("lifecycle gate blocked candidate: "
                                      + eligibility["reason"])
            else:
                try:
                    strategy_instance = create_strategy(
                        decision["strategy"], decision.get("parameters") or {})
                except (ValueError, KeyError):
                    decision["selected"] = False
                    decision["reason"] = ("selected candidate failed to "
                                          "instantiate; nothing is executed")
        if not decision.get("selected"):
            # 3.0.2: NO qualified candidate ⇒ NO entry. The fallback tuple is
            # recorded for research/diagnostics ONLY and is never executable.
            decision["executable"] = False
            decision["fallback_strategy_diagnostic"] = self.fallback[0]
            decision["fallback_parameters_diagnostic"] = dict(self.fallback[1])
        else:
            decision["executable"] = True
        self.repository.record_learning_event("ROUTER_DECISION", {
            "selected": bool(decision.get("selected")),
            "executable": bool(decision.get("executable")),
            "strategy": decision.get("strategy"),
            "parameters": decision.get("parameters"),
            "candidate_id": decision.get("candidate_id"),
            "lifecycle": decision.get("lifecycle"),
            "confidence": decision.get("confidence"),
            "reason": decision.get("reason"),
            "regime": regime,
            "candidates_considered": len(candidates),
            "degradation_penalties": sorted(degradation),
        })
        self.repository.set_engine_state("active_strategy", {
            "strategy": decision.get("strategy") if decision.get("selected")
            else None,
            "parameters": decision.get("parameters") if decision.get("selected")
            else None,
            "version": (f"{decision['strategy']}-{sorted((decision.get('parameters') or {}).items())}"
                        .replace("'", "") if decision.get("selected")
                        else "none_executable"),
            "executable": bool(decision.get("executable")),
            "confidence": decision.get("confidence"),
            "regime": regime,
            "ts": decision["ts"],
            "reason": decision.get("reason"),
        })
        return decision, strategy_instance
