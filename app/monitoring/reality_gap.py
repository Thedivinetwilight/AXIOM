"""RealityGapMonitor: does live behavior still resemble research evidence?

Compares recorded stage metrics (backtest → walk_forward → paper → sandbox).
`live` is a RECORDABLE STAGE NAME only — no code path in this build produces
it. Flagged deteriorations raise a REALITY_GAP_FLAG learning event (research
signal; risk limits are never touched from here).
"""

from __future__ import annotations

import json

STAGES = ("backtest", "walk_forward", "paper", "sandbox", "live")

FLAG_RULES = {
    "expectancy_drop": 0.5,     # later < earlier × 0.5 (with positive earlier)
    "drawdown_blowout": 1.5,    # later > earlier × 1.5 AND later > 0.02
    "frequency_collapse": 0.3,  # later trades < earlier × 0.3 (earlier ≥ 5)
}


def _load(payload):
    return json.loads(payload) if isinstance(payload, str) else payload


class RealityGapMonitor:
    def __init__(self, repository):
        self.repository = repository

    def record_stage(self, strategy: str, stage: str, metrics: dict) -> str:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage}")
        # NOTE: "live" is a valid stage NAME for completeness only. No code
        # path in this build produces live stage metrics (live trading does
        # not exist); a live row can only appear if a human records one.
        return self.repository.record_reality_gap(strategy, stage, metrics, False)

    def compare(self, strategy: str, earlier_stage: str, later_stage: str) -> dict:
        rows = [r for r in self.repository.list_reality_gaps(limit=500)
                if r["strategy"] == strategy
                and r["comparison"] in (earlier_stage, later_stage)]
        earlier = next((r for r in rows if r["comparison"] == earlier_stage), None)
        later = next((r for r in rows if r["comparison"] == later_stage), None)
        if not earlier or not later:
            return {"comparable": False,
                    "reason": "missing recorded stage metrics"}
        earlier_metrics = _load(earlier["metrics"])
        later_metrics = _load(later["metrics"])
        flags = {}
        exp_e = float(earlier_metrics.get("expectancy") or 0.0)
        exp_l = float(later_metrics.get("expectancy") or 0.0)
        if exp_e > 0 and exp_l < exp_e * FLAG_RULES["expectancy_drop"]:
            flags["expectancy_drop"] = {"earlier": exp_e, "later": exp_l}
        dd_e = float(earlier_metrics.get("max_drawdown") or 0.0)
        dd_l = float(later_metrics.get("max_drawdown") or 0.0)
        if dd_l > dd_e * FLAG_RULES["drawdown_blowout"] and dd_l > 0.02:
            flags["drawdown_blowout"] = {"earlier": dd_e, "later": dd_l}
        n_e = int(earlier_metrics.get("trades") or 0)
        n_l = int(later_metrics.get("trades") or 0)
        if n_e >= 5 and n_l < n_e * FLAG_RULES["frequency_collapse"]:
            flags["frequency_collapse"] = {"earlier": n_e, "later": n_l}
        flagged = bool(flags)
        gap_id = self.repository.record_reality_gap(
            strategy, f"{earlier_stage}_vs_{later_stage}",
            {"earlier": earlier_metrics, "later": later_metrics,
             "flags": flags}, flagged)
        if flagged:
            self.repository.record_learning_event(
                "REALITY_GAP_FLAG", {"strategy": strategy,
                                     "comparison": f"{earlier_stage}_vs_{later_stage}",
                                     "flags": flags})
        return {"comparable": True, "flagged": flagged, "flags": flags,
                "gap_id": gap_id}
