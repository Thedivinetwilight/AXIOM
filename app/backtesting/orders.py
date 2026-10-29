from dataclasses import asdict, dataclass, field
from math import isfinite
from uuid import uuid4
from app.data.models import parse_timestamp, validate_symbol

ACTIVE_STATUSES = {"NEW", "PARTIALLY_FILLED"}


@dataclass
class Order:
    symbol: str
    side: str
    quantity: float
    timestamp: str
    strategy: str = "manual"
    order_type: str = "MARKET"
    limit_price: float | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "NEW"
    filled_quantity: float = 0.0
    average_price: float = 0.0
    fees: float = 0.0
    reason: str | None = None
    quote_source: str | None = None
    quote_timestamp: str | None = None
    quote_received_at: str | None = None
    quote_id: str | None = None

    def __post_init__(self):
        validate_symbol(self.symbol)
        parse_timestamp(self.timestamp)
        if self.side not in {"BUY", "SELL"} or self.order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("Unsupported order side/type")
        if not isfinite(self.quantity) or self.quantity <= 0:
            raise ValueError("Order quantity must be finite and positive")
        if self.order_type == "LIMIT" and (
            self.limit_price is None or not isfinite(self.limit_price) or self.limit_price <= 0
        ):
            raise ValueError("Limit price must be positive")

    @property
    def remaining(self):
        return max(0.0, self.quantity - self.filled_quantity)

    def to_dict(self):
        result = asdict(self)
        result["price"] = self.average_price or self.limit_price
        return result


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    timestamp: str
    strategy: str
    reason: str = "signal"
    spread_slippage_cost: float = 0.0
    id: str = field(default_factory=lambda: uuid4().hex)
    quote_source: str | None = None
    quote_timestamp: str | None = None
    quote_received_at: str | None = None
    quote_id: str | None = None
