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
    source: Literal["replay", "binance", "generic", "upstox", "alpaca", "vantage_mt5"] = "replay"
    replay_delay: float = Field(default=0.05, ge=0, le=60)

    @model_validator(mode="after")
    def validate_alpaca_ticker(self):
        if self.source == "alpaca":
            from app.data.alpaca import validate_us_symbol

            self.symbol = validate_us_symbol(self.symbol)
        if self.source == "vantage_mt5":
            if self.strategy != "manual_only" or self.parameters:
                raise ValueError(
                    "Vantage MT5 live-price paper requires strategy=manual_only and no strategy parameters"
                )
        return self


class ManualOrderRequest(BaseModel):
    """A hand-entered paper order: simulated by the local broker, always.

    Sizing is expressed either as an absolute quantity or as a notional, never
    both: accepting both invites a silent precedence bug where the operator
    believes one number and the ticket uses the other.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str = Field(min_length=1, max_length=40)
    side: Literal["BUY", "SELL"]
    quantity: float | None = Field(default=None, gt=0, le=1e12)
    notional: float | None = Field(default=None, gt=0, le=1e15)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: float | None = Field(default=None, gt=0, le=1e12)
    idempotency_key: str | None = Field(
        default=None, min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$"
    )

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value):
        # Exact MT5 broker symbols can be case-sensitive; legacy routes apply
        # their historical uppercase normalization only outside that feed.
        return validate_symbol(value.strip())

    @model_validator(mode="after")
    def check_sizing(self) -> "ManualOrderRequest":
        if (self.quantity is None) == (self.notional is None):
            raise ValueError("supply exactly one of quantity or notional")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("a LIMIT order needs limit_price")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("a MARKET order must not carry a limit_price")
        return self
