from statistics import pstdev
from app.indicators.moving_averages import check_period


def rolling_std(values: list[float], period: int) -> list[float | None]:
    check_period(period)
    return [
        None if i < period - 1 else pstdev(values[i - period + 1 : i + 1])
        for i in range(len(values))
    ]
