"""RegimeEngine: observable market features → regime label + change detection.

Features (computed ONLY from candles available at that timestamp):
- trend: normalized close-vs-SMA distance over a window,
- volatility: stdev of close-to-close returns,
- momentum: return over a momentum window,
- volume trend: recent mean volume vs prior mean,
- time-of-day bucket (exchange-local hour).

The label reuses the V1 deterministic classifier and adds a structured
feature snapshot; regime CHANGES are recorded as learning events (audit
trail), never acted on beyond routing/monitoring.
"""

from __future__ import annotations

from statistics import fmean, pstdev

from app.history.store import parse_local
from app.learning.regime import classify_regime


def features(candles: list) -> dict:
    if len(candles) < 3:
        return {"samples": len(candles)}
    closes = [c.close for c in candles]
    volumes = [getattr(c, "volume", 0.0) for c in candles]
    window = min(len(closes), 20)
    sma = fmean(closes[-window:])
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
    vol = pstdev(returns[-window:]) if len(returns[-window:]) >= 2 else 0.0
    momentum_window = min(len(closes), 10)
    momentum = closes[-1] / closes[-momentum_window] - 1 if closes[-momentum_window] > 0 else 0.0
    recent_volume = fmean(volumes[-5:]) if volumes[-5:] else 0.0
    prior_volume = fmean(volumes[-20:-5]) if len(volumes) >= 20 and volumes[-20:-5] else recent_volume
    local_hour = parse_local(candles[-1].timestamp).hour
    tod = ("open" if local_hour < 11 else "mid" if local_hour < 14 else "close")
    return {
        "samples": len(candles),
        "trend": (closes[-1] / sma - 1) if sma > 0 else 0.0,
        "volatility": vol,
        "momentum": momentum,
        "volume_trend": (recent_volume / prior_volume - 1) if prior_volume > 0 else 0.0,
        "time_of_day": tod,
        "last_close": closes[-1],
    }


class RegimeEngine:
    def __init__(self, repository=None):
        self.repository = repository
        self.current: str | None = None

    def evaluate(self, candles: list) -> dict:
        feats = features(candles)
        label = classify_regime(candles) if len(candles) >= 3 else "insufficient_data"
        changed = self.current is not None and label != self.current
        previous = self.current
        self.current = label
        if changed and self.repository is not None:
            self.repository.record_learning_event(
                "REGIME_CHANGE",
                {"from": previous, "to": label, "features": feats})
        return {"regime": label, "features": feats,
                "changed": changed, "previous": previous}
