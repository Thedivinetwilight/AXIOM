"""Hard risk limits (V1) — enforced OUTSIDE any learning/adaptive layer.

Constructed ONLY from explicit settings values; no learning output can mutate
a HardRiskLimits instance (frozen dataclass). The engine consults the gate
before every order submission; exits/risk-reducing actions stay allowed so
limits cannot trap the account in a position.
"""

import math
from dataclasses import dataclass


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


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

    def __post_init__(self) -> None:
        for name in ("max_position_value", "max_daily_loss"):
            value = getattr(self, name)
            if not _finite_number(value):
                raise ValueError(f"{name.upper()} must be finite")
            if value < 0:
                raise ValueError(f"{name.upper()} cannot be negative")
        for name in ("max_open_positions", "max_trades_per_day"):
            value = getattr(self, name)
            if type(value) is not int:
                raise ValueError(f"{name.upper()} must be an integer")
            if value < 0:
                raise ValueError(f"{name.upper()} cannot be negative")

    @classmethod
    def from_settings(cls, settings) -> "HardRiskLimits":
        # Do not coerce/truncate here. Settings validates the declared types;
        # this boundary validates again so direct callers cannot turn 1.9 into
        # a seemingly valid one-position limit.
        return cls(
            max_position_value=settings.max_position_value,
            max_open_positions=settings.max_open_positions,
            max_daily_loss=settings.max_daily_loss,
            max_trades_per_day=settings.max_trades_per_day,
        )

    @staticmethod
    def _valid_intent(intent: OrderIntent) -> bool:
        if not isinstance(intent, OrderIntent):
            return False
        if not isinstance(intent.symbol, str) or not intent.symbol.strip():
            return False
        if intent.side not in {"BUY", "SELL"}:
            return False
        if not _finite_number(intent.quantity) or intent.quantity <= 0:
            return False
        if not _finite_number(intent.price) or intent.price <= 0:
            return False
        if not _finite_number(intent.estimated_fees) or intent.estimated_fees < 0:
            return False
        try:
            return (math.isfinite(intent.notional)
                    and math.isfinite(intent.total_exposure))
        except (OverflowError, TypeError, ValueError):
            return False

    @staticmethod
    def _valid_buy_state(*, open_positions, open_position_value,
                         trades_today, realized_today, unrealized) -> bool:
        if type(open_positions) is not int or open_positions < 0:
            return False
        if type(trades_today) is not int or trades_today < 0:
            return False
        if not _finite_number(open_position_value) or open_position_value < 0:
            return False
        if not _finite_number(realized_today) or not _finite_number(unrealized):
            return False
        try:
            return math.isfinite(float(realized_today) + float(unrealized))
        except (OverflowError, TypeError, ValueError):
            return False

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
        if not self._valid_intent(intent):
            return RiskDecision(False, "invalid_order_intent")
        # A valid risk-reducing exit is never trapped behind an invalid or
        # stale account snapshot. New exposure below must have a valid snapshot.
        if intent.side == "SELL":
            return RiskDecision(True, "risk_reducing_exit_allowed")
        if not self._valid_buy_state(
            open_positions=open_positions,
            open_position_value=open_position_value,
            trades_today=trades_today,
            realized_today=realized_today,
            unrealized=unrealized,
        ):
            return RiskDecision(False, "invalid_risk_state")

        try:
            projected_exposure = float(open_position_value) + intent.total_exposure
            daily_pnl = float(realized_today) + float(unrealized)
        except (OverflowError, TypeError, ValueError):
            return RiskDecision(False, "invalid_risk_state")
        if not math.isfinite(projected_exposure) or not math.isfinite(daily_pnl):
            return RiskDecision(False, "invalid_risk_state")

        if self.max_position_value > 0 and projected_exposure > self.max_position_value:
            return RiskDecision(False, "max_position_value_exceeded")
        if self.max_open_positions > 0 and open_positions >= self.max_open_positions:
            return RiskDecision(False, "max_open_positions_reached")
        if self.max_daily_loss > 0 and daily_pnl <= -self.max_daily_loss:
            return RiskDecision(False, "max_daily_loss_reached")
        if self.max_trades_per_day > 0 and trades_today >= self.max_trades_per_day:
            return RiskDecision(False, "max_trades_per_day_reached")
        return RiskDecision(True, "within_limits")
