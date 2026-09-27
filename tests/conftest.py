from datetime import datetime, timedelta, timezone
import pytest
from app.data.models import Candle
from app.backtesting.costs import ExecutionConfig
from scripts.make_sample_data import sample


@pytest.fixture
def bars():
    return sample(count=300)


@pytest.fixture
def zero_cost():
    return ExecutionConfig(
        initial_capital=1000,
        commission=0,
        slippage=0,
        spread=0,
        position_size=1,
        stop_loss=None,
        take_profit=None,
        max_participation=1,
    )


def candle(day=0, price=100, *, opening=None, high=None, low=None, volume=10000, symbol="DEMO"):
    opening = price if opening is None else opening
    return Candle(
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        opening,
        high if high is not None else max(opening, price),
        low if low is not None else min(opening, price),
        price,
        volume,
        symbol,
    )
