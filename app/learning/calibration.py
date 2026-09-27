"""Confidence calibration: predicted confidence vs realized outcomes.

Every routed decision records its confidence; realized outcomes close the
loop. Reliability buckets and a Brier score are computed from RECORDED rows
only. The router discounts confidence by calibration quality (documented,
bounded multiplier) — calibration can reduce confidence, never inflate it.
"""

from __future__ import annotations

from statistics import fmean

BUCKETS = ((0.0, 0.25, "<=0.25"), (0.25, 0.5, "0.25-0.5"),
           (0.5, 0.75, "0.5-0.75"), (0.75, 1.01, ">0.75"))


class ConfidenceCalibration:
    def __init__(self, repository):
        self.repository = repository

    def record(self, strategy: str, predicted: float, realized_win: bool,
               detail: dict | None = None) -> None:
        self.repository.record_calibration(strategy, predicted, realized_win,
                                           detail or {})

    def reliability(self, strategy: str) -> dict:
        rows = self.repository.list_calibration(strategy)
        if not rows:
            return {"buckets": [], "brier": None, "n": 0}
        buckets = []
        for low, high, label in BUCKETS:
            selected = [r for r in rows if low <= (r["predicted"] or 0.0) < high]
            if selected:
                buckets.append({
                    "bucket": label, "n": len(selected),
                    "observed_win_rate": fmean(bool(r["realized"]) for r in selected),
                    "mean_predicted": fmean(r["predicted"] or 0.0 for r in selected)})
        brier = fmean(((r["predicted"] or 0.0) - (1.0 if r["realized"] else 0.0)) ** 2
                      for r in rows)
        return {"buckets": buckets, "brier": round(brier, 6), "n": len(rows)}

    def discount(self, strategy: str) -> float:
        """Bounded multiplier in [0.5, 1.0]; 1.0 when uncalibrated (no data yet).

        miscalibration_cost = |observed_win_rate − mean_predicted| per bucket,
        weighted by bucket count → conservative confidence shrinkage.
        """
        stats = self.reliability(strategy)
        if not stats["buckets"]:
            return 1.0
        total = sum(b["n"] for b in stats["buckets"])
        error = sum(b["n"] * abs(b["observed_win_rate"] - b["mean_predicted"])
                    for b in stats["buckets"]) / max(total, 1)
        return max(0.5, min(1.0, 1.0 - 2.0 * error))
