"""Explainable, long-only US equity rules for the research harness.

Every candidate is intentionally small and readable: a reader must be able to
reproduce each decision with a spreadsheet. Nothing here fits parameters to
data — the parameter sets are fixed defaults, and the harness reports them
verbatim with every result.

Short trades are not modelled. A credible short backtest needs historical
borrow availability, borrow fees, and locate/recall behaviour; without those a
short leg would produce a number that cannot be defended, so the harness is
long-only by construction.

All functions receive ``history`` as ``{symbol: [Candle, ...]}`` ordered oldest
to newest and **already truncated at the decision close**, so a rule can only
look backwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, pstdev

from app.data.models import Candle


def _closes(history: list[Candle]) -> list[float]:
    return [bar.close for bar in history]


def _roc(closes: list[float], periods: int) -> float | None:
    if len(closes) <= periods or closes[-1 - periods] <= 0:
        return None
    return closes[-1] / closes[-1 - periods] - 1.0


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _zscore(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    deviation = pstdev(window)
    if deviation <= 1e-12:
        return None
    return (window[-1] - mean(window)) / deviation


def momentum_12_1(closes: list[float]) -> float | None:
    """Classic 12-month-minus-1-month total momentum (skips the last month)."""
    if len(closes) < 253:
        return None
    start, end = closes[-253], closes[-22]
    if start <= 0:
        return None
    return end / start - 1.0


@dataclass(frozen=True)
class Candidate:
    key: str
    label: str
    rationale: str
    rebalance_days: int
    min_history: int
    kind: str  # momentum | trend | breakout | mean_reversion | benchmark
    regime_filter: str | None = None  # "up" | "down" | None

    def select(
        self,
        history: dict[str, list[Candle]],
        members: set[str],
        max_positions: int,
        benchmark: list[Candle] | None = None,
    ) -> list[str]:
        if self.regime_filter and not _regime_ok(benchmark, self.regime_filter):
            return []
        scoring = {
            "momentum_trend": self._momentum_trend,
            "relative_strength": self._relative_strength,
            "moving_average": self._moving_average,
            "breakout": self._breakout,
            "mean_reversion": self._mean_reversion,
            "buy_hold": self._buy_hold,
        }[self.kind]
        ranked = scoring(history, members)
        return [symbol for symbol, _ in ranked][:max_positions]

    # Each rule returns [(symbol, score)] best-first.
    def _momentum_trend(self, history, members):
        scored = []
        for symbol in members:
            bars = history.get(symbol) or []
            score = momentum_12_1(_closes(bars))
            if score is not None and score > 0:
                scored.append((symbol, score))
        return sorted(scored, key=lambda item: item[1], reverse=True)

    def _relative_strength(self, history, members):
        scored = []
        for symbol in members:
            bars = history.get(symbol) or []
            score = momentum_12_1(_closes(bars))
            if score is not None:
                scored.append((symbol, score))
        return sorted(scored, key=lambda item: item[1], reverse=True)

    def _moving_average(self, history, members):
        scored = []
        for symbol in members:
            bars = history.get(symbol) or []
            closes = _closes(bars)
            trend = _sma(closes, 200)
            if trend is None or closes[-1] <= trend:
                continue
            score = _roc(closes, 63)
            if score is None:
                continue
            scored.append((symbol, score))
        return sorted(scored, key=lambda item: item[1], reverse=True)

    def _breakout(self, history, members):
        scored = []
        for symbol in members:
            bars = history.get(symbol) or []
            if len(bars) < 21:
                continue
            prior_high = max(bar.high for bar in bars[-21:-1])
            if prior_high <= 0 or bars[-1].close <= prior_high:
                continue
            scored.append((symbol, bars[-1].close / prior_high - 1.0))
        return sorted(scored, key=lambda item: item[1], reverse=True)

    def _mean_reversion(self, history, members):
        scored = []
        for symbol in members:
            bars = history.get(symbol) or []
            closes = _closes(bars)
            if len(closes) < 20:
                continue
            score = _zscore(closes, 20)
            if score is not None and score <= -1.0:
                scored.append((symbol, score))
        return sorted(scored, key=lambda item: item[1])  # most oversold first

    def _buy_hold(self, history, members):
        return sorted(
            (symbol, 0.0) for symbol in members if history.get(symbol)
        )


def _regime_ok(benchmark: list[Candle] | None, direction: str) -> bool:
    """Benchmark above (up) or below (down) its own 200-day simple average."""
    if not benchmark or len(benchmark) < 200:
        return False
    closes = _closes(benchmark)
    trend = _sma(closes, 200)
    if trend is None:
        return False
    return closes[-1] > trend if direction == "up" else closes[-1] < trend


MOMENTUM = Candidate(
    key="momentum_trend",
    label="Momentum / trend (12-1 month, positive only)",
    rationale=(
        "Rank the point-in-time universe by 12-month return skipping the last "
        "month (to avoid short-term reversal), hold the top N that are still "
        "positive, rebalance monthly."
    ),
    rebalance_days=21, min_history=253, kind="momentum_trend",
)

RELATIVE_STRENGTH = Candidate(
    key="relative_strength",
    label="Relative-strength ranking (12-1 month, always invested)",
    rationale=(
        "Same ranking as momentum but without the positive-return filter: the "
        "strategy is always long the top N, so it is fully exposed in falling "
        "markets. This is the control that shows whether the trend filter adds "
        "anything."
    ),
    rebalance_days=21, min_history=253, kind="relative_strength",
)

MOVING_AVERAGE = Candidate(
    key="moving_average",
    label="Moving-average trend filter (price above 200-day SMA)",
    rationale=(
        "Long only when the close is above its own 200-day simple average; rank "
        "survivors by 3-month return to pick the leaders. The most common "
        "retail trend rule, included as a benchmark to beat."
    ),
    rebalance_days=21, min_history=200, kind="moving_average",
)

BREAKOUT = Candidate(
    key="breakout",
    label="Donchian breakout (20-day high)",
    rationale=(
        "Buy names that close above their prior 20-session high, ranked by how "
        "far they broke out; exit at the monthly rebalance. A pure price-action "
        "rule with no fundamental input."
    ),
    rebalance_days=21, min_history=21, kind="breakout",
)

MEAN_REVERSION = Candidate(
    key="mean_reversion",
    label="Mean reversion (20-day z-score <= -1)",
    rationale=(
        "Buy names that are at least one standard deviation below their own "
        "20-session mean, most oversold first, hold to the next rebalance. "
        "Long-only, so it only ever fades downside."
    ),
    rebalance_days=5, min_history=20, kind="mean_reversion",
)

MOMENTUM_WITH_REGIME = Candidate(
    key="momentum_regime",
    label="Momentum with a market-regime filter",
    rationale=(
        "Momentum as above, but only when the benchmark closes above its own "
        "200-day average; otherwise hold cash. Tests whether a simple regime "
        "gate improves the net drawdown of the momentum rule."
    ),
    rebalance_days=21, min_history=253, kind="momentum_trend", regime_filter="up",
)

MEAN_REVERSION_WITH_REGIME = Candidate(
    key="mean_reversion_regime",
    label="Mean reversion with a market-regime filter",
    rationale=(
        "Mean reversion only while the benchmark is above its 200-day average "
        "(buying dips inside an uptrend); otherwise hold cash."
    ),
    rebalance_days=5, min_history=20, kind="mean_reversion", regime_filter="up",
)

BUY_HOLD = Candidate(
    key="buy_hold",
    label="Buy and hold the point-in-time universe (equal weight)",
    rationale=(
        "Equal-weight every member of the point-in-time universe and never "
        "rebalance. This is the honest no-skill comparison: a strategy that "
        "cannot beat it after costs has no claim."
    ),
    rebalance_days=21, min_history=1, kind="buy_hold",
)

CANDIDATES: tuple[Candidate, ...] = (
    BUY_HOLD,
    MOMENTUM,
    RELATIVE_STRENGTH,
    MOVING_AVERAGE,
    BREAKOUT,
    MEAN_REVERSION,
    MOMENTUM_WITH_REGIME,
    MEAN_REVERSION_WITH_REGIME,
)

BY_KEY = {candidate.key: candidate for candidate in CANDIDATES}


def get_candidate(key: str) -> Candidate:
    if key not in BY_KEY:
        raise ValueError(f"unknown candidate {key!r}; choose from {sorted(BY_KEY)}")
    return BY_KEY[key]


@dataclass
class StrategySpec:
    """Frozen, auditable description of what was actually evaluated."""

    key: str
    label: str
    rationale: str
    rebalance_days: int
    min_history: int
    regime_filter: str | None = None
    parameters: dict = field(default_factory=dict)

    @classmethod
    def of(cls, candidate: Candidate) -> "StrategySpec":
        return cls(
            key=candidate.key, label=candidate.label, rationale=candidate.rationale,
            rebalance_days=candidate.rebalance_days, min_history=candidate.min_history,
            regime_filter=candidate.regime_filter,
            parameters={"max_positions": None, "long_only": True},
        )
