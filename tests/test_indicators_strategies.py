import pytest
from app.indicators.moving_averages import sma, ema
from app.indicators.momentum import roc
from app.indicators.oscillators import rsi, macd
from app.indicators.volatility import rolling_std
from app.strategies import REGISTRY, create_strategy, catalog
from app.strategies.base import Signal
from tests.conftest import candle


def test_known_indicators():
    assert sma([1, 2, 3, 4], 3) == [None, None, 2, 3]
    assert ema([1, 2, 3, 4], 3) == [None, None, 2, 3]
    assert roc([100, 110, 121], 1)[-1] == pytest.approx(0.1)
    assert rsi(list(range(1, 30)), 14)[-1] == 100
    assert rsi(list(range(30, 1, -1)), 14)[-1] == 0
    assert rsi([100] * 30, 14)[-1] == 50
    assert rolling_std([1, 2, 3], 3)[-1] == pytest.approx((2 / 3) ** 0.5)
    line, signal = macd([100] * 60)
    assert line[-1] == signal[-1] == 0


@pytest.mark.parametrize("period", [0, -1, 1.5, True, 2001])
def test_invalid_period(period):
    with pytest.raises(ValueError):
        sma([1, 2, 3], period)


@pytest.mark.parametrize("name", list(REGISTRY))
def test_strategies_prefix_only(name, bars):
    strategy = create_strategy(name)
    assert strategy.generate_signal([]) == Signal.HOLD
    short = strategy.generate_signal(bars[:120])
    altered = bars[:120] + [candle(900, 999999)]
    assert strategy.generate_signal(altered[:120]) == short
    assert isinstance(strategy.generate_signal(bars), Signal)
    assert strategy.parameters == create_strategy(name, strategy.parameters).parameters


@pytest.mark.parametrize(
    "name,params",
    [
        ("moving_average", {"fast_period": 50, "slow_period": 20}),
        ("rsi", {"lower": 80, "upper": 20}),
        ("bollinger", {"std_dev": 0}),
        ("momentum", {"threshold": float("nan")}),
        ("macd", {"fast_period": 26, "slow_period": 12}),
        ("breakout", {"period": 0}),
        ("mean_reversion", {"deviation": -1}),
        ("moving_average", {"oops": 1}),
        ("unknown", {}),
    ],
)
def test_invalid_strategies(name, params):
    with pytest.raises(ValueError):
        create_strategy(name, params)


def test_signals():
    from app.strategies.moving_average import MovingAverageStrategy

    strategy = MovingAverageStrategy(2, 3)
    bars = [candle(i, p) for i, p in enumerate([3, 2, 1, 5])]
    assert strategy.generate_signal(bars) == Signal.BUY
    assert (
        create_strategy("momentum", {"period": 2, "threshold": 0.01}).generate_signal(bars)
        == Signal.BUY
    )
    assert create_strategy("breakout", {"period": 2}).generate_signal(bars) == Signal.BUY
    assert (
        create_strategy("rsi", {"period": 2}).generate_signal([candle(i, 10 - i) for i in range(5)])
        == Signal.BUY
    )
    assert (
        create_strategy("mean_reversion", {"period": 3}).generate_signal(
            [candle(i, p) for i, p in enumerate([100, 100, 80])]
        )
        == Signal.BUY
    )
    assert len(catalog()) == 7
