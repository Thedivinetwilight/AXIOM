"""Pure-Python trailing indicators; outputs never depend on later observations."""

from math import isfinite


def check_period(period: int):
    if isinstance(period, bool) or not isinstance(period, int) or not 1 <= period <= 2000:
        raise ValueError("period must be an integer between 1 and 2000")


def sma(values: list[float], period: int) -> list[float | None]:
    check_period(period)
    total, output = 0.0, []
    for i, value in enumerate(values):
        if not isfinite(value):
            raise ValueError("Non-finite indicator input")
        total += value
        if i >= period:
            total -= values[i - period]
        output.append(total / period if i >= period - 1 else None)
    return output


def ema(values: list[float], period: int) -> list[float | None]:
    check_period(period)
    result = [None] * len(values)
    if len(values) < period:
        return result
    state = sum(values[:period]) / period
    result[period - 1] = state
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        state += alpha * (values[i] - state)
        result[i] = state
    return result
