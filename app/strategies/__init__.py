from app.strategies.moving_average import MovingAverageStrategy
from app.strategies.momentum import MomentumStrategy
from app.strategies.mean_reversion import MeanReversionStrategy
from app.strategies.breakout import BreakoutStrategy
from app.strategies.rsi import RSIStrategy
from app.strategies.macd import MACDStrategy
from app.strategies.bollinger import BollingerStrategy

REGISTRY = {
    cls.name: cls
    for cls in (
        MovingAverageStrategy,
        MomentumStrategy,
        MeanReversionStrategy,
        BreakoutStrategy,
        RSIStrategy,
        MACDStrategy,
        BollingerStrategy,
    )
}


def create_strategy(name: str, parameters: dict | None = None):
    if name not in REGISTRY:
        raise ValueError(f"Unknown strategy: {name}")
    try:
        return REGISTRY[name](**(parameters or {}))
    except TypeError as exc:
        raise ValueError("Invalid strategy parameters") from exc


def catalog() -> list[dict]:
    return [
        {"name": name, "default_parameters": cls().parameters} for name, cls in REGISTRY.items()
    ]
