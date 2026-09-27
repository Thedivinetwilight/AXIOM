"""Cash-conserving long-only weighted-average inventory accounting."""

from dataclasses import asdict, dataclass
from uuid import uuid4
from app.backtesting.orders import Fill


@dataclass
class Position:
    symbol: str
    quantity: float
    entry_price: float
    entry_fees: float
    entry_timestamp: str
    strategy: str


class Portfolio:
    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: dict[str, Position] = {}
        self.marks: dict[str, float] = {}
        self.trades: list[dict] = []
        self.realized_pnl = 0.0
        self.fees = 0.0
        self.spread_slippage_cost = 0.0
        self.peak = initial_capital
        self.max_drawdown = 0.0
        self.day: str | None = None
        self.day_start_equity = initial_capital
        self.last_equity = initial_capital
        self.last_timestamp: str | None = None

    @property
    def equity(self):
        return self.cash + sum(
            p.quantity * self.marks.get(s, p.entry_price) for s, p in self.positions.items()
        )

    @property
    def unrealized_pnl(self):
        return sum(
            p.quantity * (self.marks.get(s, p.entry_price) - p.entry_price) - p.entry_fees
            for s, p in self.positions.items()
        )

    def apply_fill(self, fill: Fill):
        gross = fill.quantity * fill.price
        if fill.side == "BUY":
            if gross + fill.fee > self.cash + 1e-7:
                raise ValueError("Insufficient cash")
            self.cash -= gross + fill.fee
            position = self.positions.get(fill.symbol)
            if position:
                qty = position.quantity + fill.quantity
                position.entry_price = (position.quantity * position.entry_price + gross) / qty
                position.quantity = qty
                position.entry_fees += fill.fee
            else:
                self.positions[fill.symbol] = Position(
                    fill.symbol, fill.quantity, fill.price, fill.fee, fill.timestamp, fill.strategy
                )
        else:
            position = self.positions.get(fill.symbol)
            if position is None or fill.quantity > position.quantity + 1e-8:
                raise ValueError("Insufficient position quantity")
            entry_fee = position.entry_fees * fill.quantity / position.quantity
            pnl = (fill.price - position.entry_price) * fill.quantity - entry_fee - fill.fee
            self.cash += gross - fill.fee
            self.realized_pnl += pnl
            self.trades.append(
                {
                    "id": uuid4().hex,
                    "symbol": fill.symbol,
                    "entry": position.entry_price,
                    "exit": fill.price,
                    "quantity": fill.quantity,
                    "pnl": pnl,
                    "fees": entry_fee + fill.fee,
                    "strategy": position.strategy,
                    "entry_timestamp": position.entry_timestamp,
                    "exit_timestamp": fill.timestamp,
                    "reason": fill.reason,
                }
            )
            position.quantity -= fill.quantity
            position.entry_fees -= entry_fee
            if position.quantity <= 1e-8:
                del self.positions[fill.symbol]
        self.fees += fill.fee
        self.spread_slippage_cost += fill.spread_slippage_cost
        self.marks[fill.symbol] = fill.price

    def snapshot(self, timestamp: str, currency: str = "INR") -> dict:
        equity = self.equity
        if self.day != timestamp[:10]:
            self.day = timestamp[:10]
            self.day_start_equity = self.last_equity
        self.peak = max(self.peak, equity)
        drawdown = 1 - equity / self.peak
        self.max_drawdown = max(self.max_drawdown, drawdown)
        self.last_equity, self.last_timestamp = equity, timestamp
        return {
            "timestamp": timestamp,
            "currency": currency,
            "cash": self.cash,
            "equity": equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "daily_pnl": equity - self.day_start_equity,
            "drawdown": drawdown,
            "max_drawdown": self.max_drawdown,
            "fees": self.fees,
            "spread_slippage_cost": self.spread_slippage_cost,
            "exposed": bool(self.positions),
            "positions": [asdict(p) for p in self.positions.values()],
        }

    def dump(self):
        result = dict(self.__dict__)
        result["positions"] = {s: asdict(p) for s, p in self.positions.items()}
        return result

    @classmethod
    def restore(cls, state: dict):
        portfolio = cls(state["initial_capital"])
        portfolio.__dict__.update(state)
        portfolio.positions = {s: Position(**p) for s, p in state["positions"].items()}
        return portfolio
