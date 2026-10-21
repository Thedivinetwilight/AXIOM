from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.data.models import TIMEFRAMES, validate_symbol
from app.strategies import create_strategy


class StrategyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    strategy: str = "moving_average"
    parameters: dict[str, int | float] = Field(default_factory=dict)
    symbol: str = "DEMO"
    timeframe: str = "1D"
    dataset: str = Field(default="sample.csv", pattern=r"^[A-Za-z0-9_-]+\.csv$")

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value):
        return validate_symbol(value)

    @field_validator("timeframe")
    @classmethod
    def valid_timeframe(cls, value):
        if value not in TIMEFRAMES:
            raise ValueError("Unsupported timeframe")
        return value

    def build_strategy(self):
        return create_strategy(self.strategy, self.parameters)


class BacktestRequest(StrategyRequest):
    initial_capital: float = Field(default=100000, gt=0, le=1e12)
    commission: float = Field(default=0.0005, ge=0, lt=0.1)
    slippage: float = Field(default=0.0005, ge=0, lt=0.1)
    spread: float = Field(default=0.0002, ge=0, lt=0.1)
    position_size: float = Field(default=0.25, gt=0, le=1)
    stop_loss: float | None = Field(default=0.05, gt=0, lt=1)
    take_profit: float | None = Field(default=0.10, gt=0, lt=1)
    max_participation: float = Field(default=0.1, gt=0, le=1)
    annualization: int = Field(default=365, ge=1, le=366)


class PaperStartRequest(StrategyRequest):
    source: Literal["replay", "binance", "generic", "upstox"] = "replay"
    replay_delay: float = Field(default=0.05, ge=0, le=60)

    @model_validator(mode="before")
    @classmethod
    def normalize_upstox_instrument_key(cls, value):
        """Normalize the provider key at the paper-start API boundary.

        Upstox configuration/subscriptions retain ``SEGMENT|IDENTIFIER``;
        paper execution uses Arena's canonical ``SEGMENT-IDENTIFIER`` form.
        """
        if not isinstance(value, dict) or value.get("source", "replay") != "upstox":
            return value
        symbol = value.get("symbol")
        if isinstance(symbol, str) and "|" in symbol:
            normalized = dict(value)
            normalized["symbol"] = symbol.replace("|", "-", 1)
            return normalized
        return value
