from app.indicators.moving_averages import check_period, ema


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI; flat prices = 50, gains only = 100, losses only = 0."""
    check_period(period)
    result = [None] * len(values)
    if len(values) <= period:
        return result
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(0, c) for c in changes[:period]) / period
    loss = sum(max(0, -c) for c in changes[:period]) / period

    def value():
        return 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)

    result[period] = value()
    for i in range(period + 1, len(values)):
        gain = (gain * (period - 1) + max(0, changes[i - 1])) / period
        loss = (loss * (period - 1) + max(0, -changes[i - 1])) / period
        result[i] = value()
    return result


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ma, slow_ma = ema(values, fast), ema(values, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(fast_ma, slow_ma)]
    valid = [v for v in line if v is not None]
    signal_values = ema(valid, signal)
    aligned = [None] * (len(values) - len(valid)) + signal_values
    return line, aligned
