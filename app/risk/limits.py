"""Hard risk limits (V1) — enforced OUTSIDE any learning/adaptive layer.

Constructed ONLY from explicit settings values; no learning output can mutate
a HardRiskLimits instance (frozen dataclass). The engine consults the gate
before every order submission; exits/risk-reducing actions stay allowed so
limits cannot trap the account in a position.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str  # BUY | SELL
    quantity: float
    price: float  # reference price for exposure math (mark or limit)
    instrument_key: str = ""  # authoritative provider instrument id (3.0.1)
    estimated_fees: float = 0.0  # fees/slippage-aware exposure (3.0.1)

    @property
    def notional(self) -> float:
        return abs(float(self.quantity) * float(self.price))

    @property
    def total_exposure(self) -> float:
        """Notional PLUS estimated execution costs (no hidden fee violations)."""
        return self.notional + abs(float(self.estimated_fees))


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class HardRiskLimits:
    max_position_value: float = 0.0  # 0 = unlimited
    max_open_positions: int = 0      # 0 = unlimited
    max_daily_loss: float = 0.0      # 0 = disabled
    max_trades_per_day: int = 0      # 0 = unlimited

    @classmethod
    def from_settings(cls, settings) -> "HardRiskLimits":
        return cls(
            max_position_value=float(settings.max_position_value),
            max_open_positions=int(settings.max_open_positions),
            max_daily_loss=float(settings.max_daily_loss),
            max_trades_per_day=int(settings.max_trades_per_day),
        )

    def check(
        self,
        intent: OrderIntent,
        *,
        open_positions: int,
        open_position_value: float,
        trades_today: int,
        realized_today: float,
        unrealized: float = 0.0,
    ) -> RiskDecision:
        """Gate one NEW order (risk-reducing SELLs always pass position caps)."""
        if intent.side == "SELL":
            return RiskDecision(True, "risk_reducing_exit_allowed")
        if self.max_position_value > 0 and (
            open_position_value + intent.notional > self.max_position_value
        ):
            return RiskDecision(False, "max_position_value_exceeded")
        if self.max_open_positions > 0 and open_positions >= self.max_open_positions:
            return RiskDecision(False, "max_open_positions_reached")
        if self.max_daily_loss > 0 and (realized_today + unrealized) <= -self.max_daily_loss:
            return RiskDecision(False, "max_daily_loss_reached")
        if self.max_trades_per_day > 0 and trades_today >= self.max_trades_per_day:
            return RiskDecision(False, "max_trades_per_day_reached")
        return RiskDecision(True, "within_limits")
