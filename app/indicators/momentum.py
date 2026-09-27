from app.indicators.moving_averages import check_period


def roc(values: list[float], period: int) -> list[float | None]:
    check_period(period)
    return [None if i < period else values[i] / values[i - period] - 1 for i in range(len(values))]
