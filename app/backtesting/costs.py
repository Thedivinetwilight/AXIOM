from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class ExecutionConfig:
    initial_capital: float = 100_000
    commission: float = 0.0005  # Proportional fee per side
    slippage: float = 0.0005
    spread: float = 0.0002  # Full proportional spread; half is paid per side
    position_size: float = 0.25  # Target allocation per symbol, not leverage
    stop_loss: float | None = 0.05
    take_profit: float | None = 0.10
    max_participation: float = 0.10  # Fraction of bar volume shared by all fills

    def __post_init__(self):
        if not isfinite(self.initial_capital) or self.initial_capital <= 0:
            raise ValueError("initial_capital must be finite and positive")
        for name in ("commission", "slippage", "spread"):
            value = getattr(self, name)
            if not isfinite(value) or not 0 <= value < 0.1:
                raise ValueError(f"{name} must be in [0, 0.1)")
        for name in ("position_size", "max_participation"):
            value = getattr(self, name)
            if not isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        for name in ("stop_loss", "take_profit"):
            value = getattr(self, name)
            if value is not None and (not isfinite(value) or not 0 < value < 1):
                raise ValueError(f"{name} must be None or in (0, 1)")

    def fill_price(self, reference: float, side: str) -> float:
        adjustment = self.slippage + self.spread / 2
        return reference * (1 + adjustment if side == "BUY" else 1 - adjustment)

    def fee(self, quantity: float, price: float) -> float:
        return quantity * price * self.commission
