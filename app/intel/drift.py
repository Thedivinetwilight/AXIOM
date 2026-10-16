"""Distribution-drift detector for instrument windows (master spec §25).

Deterministic comparison of two consecutive windows of OBSERVED closes:
location (mean), dispersion (stdev of returns) and tail behaviour (max
absolute return). Every verdict carries the numbers it was computed from —
never a bare label.

Verdicts:
* ``INSUFFICIENT_DATA`` — fewer than ``min_samples`` closes in either
  window; no claim is made.
* ``STABLE`` — every statistic stayed inside its tolerance.
* ``DRIFT_DETECTED`` — at least one statistic moved beyond tolerance.

Tolerances are relative (fraction of the baseline window's magnitude) with
an absolute floor so a quiet baseline cannot manufacture false alarms.
The detector never repairs or interpolates data; bad input raises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, pstdev


@dataclass(frozen=True)
class DriftReport:
    identity: str
    verdict: str                 # STABLE | DRIFT_DETECTED | INSUFFICIENT_DATA
    baseline_samples: int
    recent_samples: int
    statistics: dict             # name -> {"baseline": x, "recent": y, "shift": z}
    drifted: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict:
        return {"identity": self.identity, "verdict": self.verdict,
                "baseline_samples": self.baseline_samples,
                "recent_samples": self.recent_samples,
                "statistics": self.statistics, "drifted": list(self.drifted),
                "detail": self.detail}


def _validate(closes: list[float]) -> None:
    for value in closes:
        if not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
            raise ValueError("drift detection refuses non-finite closes")
        if value <= 0:
            raise ValueError("drift detection refuses non-positive closes")


def _returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]


class DriftDetector:
    def __init__(self, *, window: int = 30, min_samples: int = 20,
                 relative_tolerance: float = 0.5, absolute_floor: float = 1e-4):
        if window < 4:
            raise ValueError("window must cover at least 4 closes")
        self.window = int(window)
        self.min_samples = int(min_samples)
        self.relative_tolerance = float(relative_tolerance)
        self.absolute_floor = float(absolute_floor)

    def evaluate(self, identity: str, closes: list[float]) -> DriftReport:
        _validate(list(closes))
        baseline = closes[-2 * self.window:-self.window] if len(closes) >= 2 * self.window else []
        recent = closes[-self.window:]
        if len(baseline) < self.min_samples or len(recent) < self.min_samples:
            return DriftReport(
                identity, "INSUFFICIENT_DATA", len(baseline), len(recent),
                {}, (),
                f"need >= {self.min_samples} closes in both windows "
                f"(baseline {len(baseline)}, recent {len(recent)})")
        base_returns = _returns(baseline)
        recent_returns = _returns(recent)
        base_mean = fmean(baseline)
        base_vol = pstdev(base_returns)
        # Location tolerance is a 3-sigma band of the BASELINE return noise,
        # scaled to the price level — a level shift is judged against how
        # noisy the baseline itself was, never against the price magnitude.
        location_tolerance = max(self.absolute_floor,
                                 3.0 * base_vol * abs(base_mean))
        candidates = {
            "location": (base_mean, fmean(recent), location_tolerance),
            "dispersion": (base_vol, pstdev(recent_returns),
                           max(self.absolute_floor,
                               self.relative_tolerance * base_vol)),
            "tail": (max(abs(r) for r in base_returns),
                     max(abs(r) for r in recent_returns),
                     max(self.absolute_floor,
                         self.relative_tolerance * max(abs(r) for r in base_returns))),
        }
        statistics: dict[str, dict] = {}
        drifted: list[str] = []
        for name, (base_value, recent_value, tolerance) in candidates.items():
            shift = (recent_value - base_value) / tolerance if tolerance > 0 else 0.0
            statistics[name] = {"baseline": round(base_value, 8),
                                "recent": round(recent_value, 8),
                                "tolerance": round(tolerance, 8),
                                "shift_in_tolerances": round(shift, 3)}
            if abs(recent_value - base_value) > tolerance:
                drifted.append(name)
        verdict = "DRIFT_DETECTED" if drifted else "STABLE"
        detail = (f"{', '.join(drifted)} drifted beyond tolerance"
                  if drifted else "all tracked statistics within tolerance")
        return DriftReport(identity, verdict, len(baseline), len(recent),
                           statistics, tuple(drifted), detail)
