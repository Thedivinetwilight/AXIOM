"""ResearchDirector: turns recorded evidence into research questions.

The director surveys recorded state (degradation, parameter instability,
regime coverage, data coverage gaps, unexplained P&L days), persists each
question once (idempotent by exact question text), and can schedule an
experiment for a question. It CANNOT bypass risk limits or execute live —
it only records questions and experiments.
"""

from __future__ import annotations

import hashlib
import json
import uuid


def _load(payload):
    return json.loads(payload) if isinstance(payload, str) else (payload or {})


class ResearchDirector:
    def __init__(self, repository, *, experiment_engine=None):
        self.repository = repository
        self.experiment_engine = experiment_engine

    def survey(self) -> list[dict]:
        questions = []
        # 1. degraded / degraded-pending strategies
        for lineage in self.repository.list_lineage():
            current = self.repository.current_lifecycle(lineage["id"])
            if current and current["status"] in {"PROBATION", "SHADOW", "RETIRED"}:
                questions.append({
                    "question": f"Why is {lineage['strategy']} degrading "
                                f"({current['status']})?",
                    "rationale": {"kind": "degradation", "lineage_id": lineage["id"],
                                  "evidence": _load(current.get("evidence"))}})
        # 2. parameter instability
        for candidate in self.repository.list_candidates(limit=100):
            payload = _load(candidate.get("robustness"))
            if float(payload.get("score") or 0.0) < 0.3:
                questions.append({
                    "question": f"Is {candidate['strategy']} parameter-robust?",
                    "rationale": {"kind": "parameter_instability",
                                  "candidate": candidate["id"],
                                  "robustness": payload.get("score")}})
        # 3. regimes without qualified candidates
        qualified = {c["strategy"] for c in self.repository.list_candidates(
            promoted=True, limit=100)}
        if not qualified:
            questions.append({
                "question": "Which regime has the strongest unexploited evidence?",
                "rationale": {"kind": "regime_gap",
                              "detail": "no qualified candidates recorded yet"}})
        # 4. coverage gaps for known instruments
        covered = {row["instrument_key"] for row in self.repository.list_coverage(limit=1000)}
        missing = [key for key in self.repository.all_instrument_keys()[:200]
                   if key not in covered]
        if missing:
            questions.append({
                "question": f"Should we collect history for {len(missing)}+ "
                            "instruments with no local coverage?",
                "rationale": {"kind": "coverage_gap", "sample": missing[:5]}})
        # 5. unexplained P&L days
        snapshots = self.repository.list_daily_snapshots(limit=30)
        if len(snapshots) >= 4:
            pnls = [float(s["realized"] or 0) for s in snapshots]
            average = sum(pnls) / len(pnls)
            worst = min(snapshots, key=lambda s: float(s["realized"] or 0))
            if float(worst["realized"] or 0) < 2 * average - 0.001 \
                    and not worst.get("regime"):
                questions.append({
                    "question": f"What drove the P&L outlier on {worst['date']}?",
                    "rationale": {"kind": "unexplained_pnl",
                                  "realized": worst["realized"]}})
        # persist new questions (idempotent by exact question text)
        existing = {q["question"] for q in self.repository.list_research_questions(limit=500)}
        created = []
        for item in questions:
            if item["question"] in existing:
                continue
            question_id = self.repository.record_research_question(
                item["question"], item["rationale"])
            created.append({"id": question_id, **item})
        return created

    def schedule(self, question_id: str, *, candles=None,
                 timeframe: str = "1m") -> dict:
        rows = self.repository.list_research_questions(limit=500)
        question = next((q for q in rows if q["id"] == question_id), None)
        if question is None:
            return {"scheduled": False, "reason": "unknown question id"}
        if candles is None or self.experiment_engine is None:
            self.repository.update_research_question(question_id, status="blocked",
                                                     experiment_id=None)
            return {"scheduled": False,
                    "reason": "no local data/experiment engine available; recorded as blocked"}
        # Python's built-in hash() is randomized per process (PYTHONHASHSEED);
        # derive the seed deterministically from the question text via SHA-256
        # so experiments are reproducible across restarts and hosts.
        seed = int.from_bytes(
            hashlib.sha256(question["question"].encode("utf-8")).digest()[:8],
            "big") % (2 ** 31)
        experiment_id = uuid.uuid4().hex
        self.repository.record_experiment(experiment_id, f"director:{question_id}", {
            "question": question["question"], "seed": seed,
            "bars": len(candles), "timeframe": timeframe})
        self.repository.update_research_question(question_id, status="scheduled",
                                                 experiment_id=experiment_id)
        return {"scheduled": True, "experiment_id": experiment_id, "seed": seed}
