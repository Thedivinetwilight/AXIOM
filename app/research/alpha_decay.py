"""Alpha Decay — measures signal quality vs age, per strategy/event.

Tracks signal age, subsequent return, success prob, expectancy by age, vol by age.
Allows each strategy/event to have learned decay profile. Minimum samples, OOS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class DecayPoint:
    age: int
    trades: int
    win_rate: float
    expectancy: float
    volatility: float


@dataclass
class DecayProfile:
    strategy: str
    points: List[DecayPoint] = field(default_factory=list)
    half_life: float | None = None
    stable: bool = True


class AlphaDecayTracker:
    def __init__(self, min_samples: int = 20, max_age: int = 10):
        self.min_samples = int(min_samples)
        self.max_age = int(max_age)
        self._records: List[dict] = []  # {strategy, age, return, ts}

    @classmethod
    def from_settings(cls, settings):
        return cls(min_samples=int(getattr(settings, "alpha_decay_min_samples", 20)))

    def record(self, strategy: str, age: int, ret: float, ts: str | None = None):
        if age < 0 or age > self.max_age:
            return
        if ret is None or math.isnan(ret) or math.isinf(ret):
            return
        self._records.append({"strategy": strategy, "age": int(age), "return": float(ret), "ts": ts})
        if len(self._records) > 5000:
            self._records = self._records[-5000:]

    def profile(self, strategy: str) -> DecayProfile:
        recs = [r for r in self._records if r["strategy"] == strategy]
        if len(recs) < self.min_samples:
            return DecayProfile(strategy, [], None, True)

        # Group by age
        by_age: Dict[int, List[float]] = {}
        for r in recs:
            by_age.setdefault(r["age"], []).append(r["return"])

        points = []
        for age in sorted(by_age.keys()):
            rets = by_age[age]
            if len(rets) < max(3, self.min_samples // 4):
                continue
            wins = sum(1 for v in rets if v > 0)
            win_rate = wins / len(rets) if rets else 0
            exp = sum(rets) / len(rets) if rets else 0
            # volatility
            mean = exp
            var = sum((v - mean) ** 2 for v in rets) / len(rets) if len(rets) > 1 else 0
            vol = math.sqrt(var) if var > 0 else 0
            points.append(DecayPoint(age, len(rets), win_rate, exp, vol))

        # Determine stability and half-life
        stable = True
        half_life = None
        if len(points) >= 2:
            # If expectancy drops >50% from age 0 to age 5, consider decaying
            e0 = next((p.expectancy for p in points if p.age == 0), None)
            e5 = next((p.expectancy for p in points if p.age == 5), None)
            if e0 is not None and e5 is not None and e0 > 0:
                if e5 < e0 * 0.5:
                    stable = False
                    # simple half-life estimate: linear between 0 and 5
                    # Find age where expectancy ~ e0/2
                    for p in points:
                        if p.expectancy <= e0 * 0.5:
                            half_life = float(p.age)
                            break
                elif e0 > 0 and e5 < 0:
                    stable = False
                    half_life = 5.0
            # Also check win_rate decay
            if points[0].win_rate > 0.6 and points[-1].win_rate < 0.45:
                stable = False

        # Don't invalidate from 1-2 samples
        if len(recs) < self.min_samples:
            stable = True

        return DecayProfile(strategy, points, half_life, stable)

    def decay_factor(self, strategy: str, age: int) -> float:
        prof = self.profile(strategy)
        if not prof.points:
            return 1.0
        # Find point for age or interpolate
        for p in prof.points:
            if p.age == age:
                # factor = expectancy at age / expectancy at 0, clamped
                e0 = next((q.expectancy for q in prof.points if q.age == 0), None)
                if e0 is None or e0 <= 0:
                    return 1.0
                if p.expectancy <= 0:
                    return 0.5
                return max(0.5, min(1.0, p.expectancy / e0))
        # Not found, use nearest
        # If age beyond max, use last
        if prof.points and age > prof.points[-1].age:
            e0 = prof.points[0].expectancy
            elast = prof.points[-1].expectancy
            if e0 <= 0:
                return 1.0
            return max(0.5, min(1.0, elast / e0 if elast else 0.5))
        return 1.0

    def should_trade(self, strategy: str, age: int) -> tuple[bool, str]:
        if age < 0:
            return False, "invalid_age"
        if len([r for r in self._records if r["strategy"] == strategy]) < self.min_samples:
            return True, "insufficient_sample"
        factor = self.decay_factor(strategy, age)
        if factor < 0.6:
            return False, f"decayed_factor {factor:.2f}"
        return True, f"factor {factor:.2f}"
