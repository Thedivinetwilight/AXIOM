"""Market-regime labels from OBSERVABLE market data only (V1).

Deterministic, transparent rules over the recent candle window:
- trend: sign of (last close − SMA(trend_window)) and its magnitude vs a
  fraction of realized volatility,
- volatility: realized volatility of simple returns vs absolute thresholds.

No future data, no provider internals, no fitted models: anyone can recompute
the label from persisted candles, which keeps every journal entry auditable.
"""

from statistics import fmean, pstdev

from app.data.models import Candle

REGIME_WINDOW = 20
TREND_FRACTION = 0.5  # |return over window| must exceed this × vol to be a trend
VOL_LOW = 0.001       # per-candle realized-vol thresholds (configurable constants,
VOL_HIGH = 0.003      # documented; learning may NOT change risk behavior with them)


def realized_volatility(candles: list[Candle]) -> float:
    if len(candles) < 3:
        return 0.0
    closes = [c.close for c in candles]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
    return pstdev(returns) if len(returns) >= 2 else 0.0


def classify_regime(candles: list[Candle], *, window: int = REGIME_WINDOW,
                    trend_fraction: float = TREND_FRACTION,
                    vol_low: float = VOL_LOW, vol_high: float = VOL_HIGH) -> str:
    """Label like 'trend_up_normal', 'range_low_vol', 'trend_down_high_vol'."""
    recent = candles[-window:] if candles else []
    if len(recent) < 3:
        return "insufficient_data"
    closes = [c.close for c in recent]
    mean = fmean(closes)
    if mean <= 0:
        return "insufficient_data"
    window_return = closes[-1] / mean - 1
    vol = realized_volatility(recent)
    if vol >= vol_high:
        volatility = "high_vol"
    elif vol <= vol_low:
        volatility = "low_vol"
    else:
        volatility = "normal"
    if abs(window_return) <= trend_fraction * max(vol, 1e-9):
        return f"range_{volatility}"
    return f"trend_up_{volatility}" if window_return > 0 else f"trend_down_{volatility}"
