"""Historical analogue engine (master spec section 10).

Given the current feature state of an instrument, search recorded history for
windows whose features resemble it, and report what happened NEXT in each
analogue window. Honest by construction:

* similarity is a documented distance over explicitly listed features;
* every analogue reports its window, similarity, features and outcome;
* results include NEGATIVE analogues (dissimilar windows) on request, so the
  evidence is never cherry-picked toward the recommendation;
* sample size, methodology and limitations are part of the result;
* if there is not enough history the engine says so instead of guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_WINDOW = 20          # bars needed to compute features
MIN_ANALOGUES = 3        # below this we refuse to summarize


@dataclass(frozen=True)
class AnalogueResult:
    analogue_start_index: int
    similarity: float
    features: dict
    forward_return: float | None
    forward_volatility: float | None

    def to_dict(self) -> dict:
        return {"analogue_start_index": self.analogue_start_index,
                "similarity": round(self.similarity, 4),
                "features": {k: round(v, 4) for k, v in self.features.items()},
                "forward_return": None if self.forward_return is None
                else round(self.forward_return, 6),
                "forward_volatility": None if self.forward_volatility is None
                else round(self.forward_volatility, 6)}


@dataclass
class AnalogueSearch:
    identity: str
    current_features: dict
    results: list[AnalogueResult] = field(default_factory=list)
    negatives: list[AnalogueResult] = field(default_factory=list)
    sample_size: int = 0
    methodology: str = ""
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "identity": self.identity,
            "current_features": {k: round(v, 4) for k, v in self.current_features.items()},
            "results": [r.to_dict() for r in self.results],
            "negatives": [r.to_dict() for r in self.negatives],
            "sample_size": self.sample_size,
            "methodology": self.methodology,
            "limitations": self.limitations,
            "enough_history": self.sample_size >= MIN_ANALOGUES,
        }


class AnalogueEngine:
    """Feature-based nearest-window search over a close/volume series."""

    def __init__(self, *, lookback: int = 20, horizon: int = 5, top_k: int = 5):
        if lookback < MIN_WINDOW:
            raise ValueError(f"lookback must be >= {MIN_WINDOW}")
        if horizon < 1:
            raise ValueError("horizon must be >= 1")
        self.lookback = lookback
        self.horizon = horizon
        self.top_k = top_k

    # ------------------------------------------------------------- features --
    def features_for(self, closes: list[float], volumes: list[float] | None = None,
                     end: int | None = None) -> dict:
        """Observable features of the window ending at ``end`` (inclusive)."""
        end = len(closes) - 1 if end is None else end
        if end < self.lookback:
            raise ValueError(f"need at least {self.lookback + 1} closes, got {end + 1}")
        window = closes[end - self.lookback + 1:end + 1]
        self._validate(window)
        ret = (window[-1] - window[0]) / window[0]
        step_returns = [(window[i] - window[i - 1]) / window[i - 1]
                      for i in range(1, len(window))]
        vol = _std(step_returns)
        mean_abs = sum(abs(r) for r in step_returns) / len(step_returns) or 1e-12
        trend = _slope(step_returns)
        position = (window[-1] - min(window)) / ((max(window) - min(window)) or 1e-12)
        features = {"return": ret, "volatility": vol, "trend_strength": trend,
                    "relative_position": position}
        if volumes:
            vol_window = volumes[end - self.lookback + 1:end + 1]
            if len(vol_window) == len(window) and sum(vol_window) > 0:
                half = self.lookback // 2
                recent = sum(vol_window[-half:]) / half
                earlier = sum(vol_window[:-half]) / max(1, len(vol_window) - half)
                features["volume_acceleration"] = (recent - earlier) / (earlier or 1e-12)
        return features

    def _validate(self, window: list[float]) -> None:
        for value in window:
            if not isinstance(value, (int, float)) or math.isnan(value) \
                    or math.isinf(value) or value <= 0:
                raise ValueError("analogue features require positive, finite closes")

    # --------------------------------------------------------------- search --
    def search(self, identity: str, closes: list[float],
               volumes: list[float] | None = None, *,
               include_negatives: bool = True) -> AnalogueSearch:
        """Find historical windows resembling the CURRENT trailing window.

        The current window is excluded from candidates (no self-match / no
        look-ahead: candidates must end at least ``horizon`` bars before the
        current window starts, so their forward outcome is fully observed).
        """
        n = len(closes)
        if n < self.lookback + 1:
            return AnalogueSearch(identity=identity, current_features={},
                                  methodology=self._methodology(),
                                  limitations=["insufficient history for features"])
        current = self.features_for(closes, volumes)
        # Candidate windows must end early enough that their forward outcome
        # (``horizon`` bars) lies fully inside recorded history AND does not
        # overlap the current window -> no look-ahead leakage.
        last_candidate_end = n - 1 - self.horizon - self.lookback
        results: list[tuple[float, AnalogueResult]] = []
        for end in range(self.lookback, last_candidate_end + 1):
            features = self.features_for(closes, volumes, end=end)
            distance = self._distance(current, features)
            forward_end = min(end + self.horizon, n - 1)
            forward_return = (closes[forward_end] - closes[end]) / closes[end]
            fwd_steps = [(closes[i] - closes[i - 1]) / closes[i - 1]
                         for i in range(end + 1, forward_end + 1)]
            forward_vol = _std(fwd_steps) if fwd_steps else None
            results.append((distance, AnalogueResult(
                analogue_start_index=end - self.lookback + 1,
                similarity=1.0 / (1.0 + distance),
                features=features, forward_return=forward_return,
                forward_volatility=forward_vol)))
        results.sort(key=lambda item: item[0])
        search = AnalogueSearch(identity=identity, current_features=current,
                                methodology=self._methodology())
        search.results = [r for _, r in results[:self.top_k]]
        if include_negatives and len(results) > self.top_k:
            search.negatives = [r for _, r in results[-min(2, len(results) - self.top_k):]]
        search.sample_size = len(results)
        if search.sample_size < MIN_ANALOGUES:
            search.limitations.append(
                f"only {search.sample_size} candidate windows; treat as anecdotal")
        search.limitations.append(
            "analogues are contextual evidence, not a guarantee; outcomes depend "
            "on conditions that may not repeat")
        return search

    def _distance(self, a: dict, b: dict) -> float:
        keys = sorted(set(a) & set(b))
        if not keys:
            return float("inf")
        total = 0.0
        for key in keys:
            da, db = float(a[key]), float(b[key])
            scale = max(abs(da), abs(db), 1e-9)
            total += ((da - db) / scale) ** 2
        return math.sqrt(total / len(keys))

    def _methodology(self) -> str:
        return (f"normalized euclidean distance over features of the trailing "
                f"{self.lookback}-bar window; forward outcome measured over the "
                f"next {self.horizon} bars; candidates excluded from overlapping "
                "the current window (no look-ahead)")

    # -------------------------------------------------------------- persist --
    def summarize(self, search: AnalogueSearch) -> dict:
        """Aggregate outcome stats over the matched analogues (when enough)."""
        if len(search.results) < MIN_ANALOGUES:
            return {"summarizable": False,
                    "reason": f"{len(search.results)} analogues < {MIN_ANALOGUES}"}
        returns = [r.forward_return for r in search.results
                   if r.forward_return is not None]
        if not returns:
            return {"summarizable": False, "reason": "no observed forward outcomes"}
        positive = sum(1 for r in returns if r > 0)
        return {
            "summarizable": True,
            "sample_size": len(returns),
            "mean_forward_return": round(sum(returns) / len(returns), 6),
            "positive_fraction": round(positive / len(returns), 4),
            "note": "historical distribution only; not a forecast",
        }


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    n = len(values)
    xs = range(n)
    xbar = (n - 1) / 2
    ybar = sum(values) / n
    denom = sum((x - xbar) ** 2 for x in xs) or 1.0
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, values)) / denom
    scale = max(abs(ybar), 1e-9)
    return slope / scale
