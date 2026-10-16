"""Missed-opportunity research + expired-candidate recorder (spec §11, §24).

Two recorders share one durable store:

* MissedOpportunityRecorder — "what did we miss?": false negatives, late
  promotions, correct-but-unexecuted recommendations, safety-blocked entries.
  Every record keeps what was visible at the time, what would have changed
  the decision, whether it was realistically knowable, the counterfactual and
  the research question it generates. No hindsight leakage: records store the
  evidence AVAILABLE THEN, not the final outcome as if known.

* ExpiredCandidateRecorder — the latency half of section 24: opportunities
  that expired before the system could act, with the measured ages, so
  deadline misses are visible as product data.
"""

from __future__ import annotations

import time


class MissedOpportunityRecorder:
    KINDS = ("false_negative", "late_promotion", "execution_failed",
             "blocked_by_safety", "model_disagreed", "analogue_would_help")

    def __init__(self, repository, *, clock=time.time):
        self.repository = repository
        self.clock = clock

    def record(self, *, kind: str, payload: dict, knowable: bool,
               market_id: str | None = None, identity: str | None = None,
               counterfactual: str | None = None,
               research_question: str | None = None) -> str:
        if kind not in self.KINDS:
            raise ValueError(f"unknown missed-opportunity kind '{kind}'")
        payload = dict(payload)
        payload.setdefault("recorded_ts", self.clock())
        return self.repository.record_missed_opportunity(
            kind=kind, payload=payload, knowable=knowable, market_id=market_id,
            identity=identity, counterfactual=counterfactual,
            research_question=research_question)

    def list(self, limit: int = 100) -> list[dict]:
        return self.repository.list_missed_opportunities(limit=limit)

    def stats(self) -> dict:
        rows = self.repository.list_missed_opportunities(limit=500)
        by_kind: dict[str, int] = {}
        knowable = 0
        for row in rows:
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
            if row.get("knowable"):
                knowable += 1
        return {"total": len(rows), "by_kind": by_kind, "knowable": knowable,
                "note": "knowable = the missing information was realistically "
                        "available at decision time"}


class ExpiredCandidateRecorder:
    """Candidates that died of old age before action (latency observability)."""

    def __init__(self, missed: MissedOpportunityRecorder):
        self.missed = missed

    def record_expired(self, *, opportunity: dict, reason: str = "expired_before_action") -> str:
        payload = {
            "opportunity_id": opportunity.get("id"),
            "state_at_expiry": opportunity.get("state"),
            "age_seconds": opportunity.get("age_seconds"),
            "evidence_age_seconds": opportunity.get("evidence_age_seconds"),
            "reason": reason,
        }
        return self.missed.record(
            kind="late_promotion", payload=payload, knowable=True,
            market_id=opportunity.get("market_id"),
            identity=opportunity.get("identity"),
            counterfactual="candidate expired before any recommendation/action",
            research_question="why did the pipeline not act or downgrade sooner?")
