"""Paper is the only implemented broker. No SDK or real order URL exists."""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import logging
from app.backtesting.costs import ExecutionConfig
from app.backtesting.orders import ACTIVE_STATUSES, Fill, Order
from app.backtesting.portfolio import Portfolio
from app.data.models import parse_timestamp
from app.data.validator import validate
from app.paper_trading.execution import ContinuousCalendar, execution_price, calendar_signature

logger = logging.getLogger("bot.broker")


class BrokerInterface(ABC):
    @abstractmethod
    def place_order(self, order: Order) -> dict: ...
    @abstractmethod
    def cancel_order(self, order_id: str) -> dict: ...
    @abstractmethod
    def get_order(self, order_id: str) -> dict: ...
    @abstractmethod
    def get_positions(self) -> list[dict]: ...
    @abstractmethod
    def get_balance(self) -> dict: ...
    @abstractmethod
    def get_orders(self) -> list[dict]: ...


class FutureRealBroker(BrokerInterface):
    """Non-operational architectural marker. Cannot be instantiated.

    Real-money routing is NOT done through this marker: it lives in
    ``app.brokers.live.UpstoxLiveBroker``, behind the four-switch opt-in in
    ``app/config/settings.py``. This class stays non-instantiable so the paper
    package can never be mistaken for an execution path.
    """

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "This marker is not an execution path; real-money routing is "
            "app/brokers/live.py behind the gated MODE=LIVE + LIVE_TRADING=true "
            "+ BROKER=UPSTOX_LIVE + LIVE_CONFIRM opt-in")


class PaperBroker(BrokerInterface):
    def __init__(
        self,
        config: ExecutionConfig | None = None,
        *,
        currency: str = "INR",
        timeframe: str = "1D",
        calendar=None,
        repository=None,
        restore=True,
    ):
        from app.data.models import TIMEFRAMES

        if timeframe not in TIMEFRAMES:
            raise ValueError("Unsupported timeframe")
        self.config = config or ExecutionConfig()
        self.currency, self.timeframe = currency, timeframe
        self.calendar = calendar or ContinuousCalendar()
        self.portfolio = Portfolio(self.config.initial_capital)
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.last_bars: dict[str, str] = {}
        self.last_candles: dict[str, dict] = {}
        self.context: dict = {}  # Persist strategy warmup/controller metadata with account.
        self.repository = repository
        if repository:
            repository.acquire_writer()
            try:
                state = repository.load_broker() if restore else None
                if state:
                    # One comparison, shared with the launcher's preflight and
                    # diagnostics: the refusal names the exact field(s) that
                    # differ instead of an opaque "config differs".
                    from app.paper_trading.compatibility import (
                        compare_saved_paper_state,
                    )

                    mismatch = compare_saved_paper_state(
                        state,
                        config=self.config,
                        currency=currency,
                        timeframe=timeframe,
                        calendar=self.calendar,
                        path=str(getattr(getattr(repository, "db", None),
                                         "path", "") or ""),
                    )
                    if mismatch is not None:
                        raise mismatch
                    self.portfolio = Portfolio.restore(state["portfolio"])
                    self.orders = {o["id"]: Order(**o) for o in state["orders"]}
                    self.fills = [Fill(**f) for f in state["fills"]]
                    self.last_candles = state.get("last_candles", {})
                    self.last_bars, self.context = state["last_bars"], state.get("context", {})
            except Exception:
                repository.release_writer()
                raise

    def _persist(self, snapshot=None):
        if self.repository:
            self.repository.save_broker(self, snapshot)

    def dump(self):
        return {
            "config": asdict(self.config),
            "calendar": calendar_signature(self.calendar),
            "currency": self.currency,
            "timeframe": self.timeframe,
            "portfolio": self.portfolio.dump(),
            "orders": [asdict(o) for o in self.orders.values()],
            "fills": [asdict(f) for f in self.fills],
            "last_bars": self.last_bars,
            "last_candles": self.last_candles,
            "context": self.context,
        }

    def place_order(self, order: Order) -> dict:
        # Never trust mutable caller-owned order objects or pre-filled statuses.
        order = deepcopy(order)
        if order.id in self.orders:
            raise ValueError("Duplicate order id")
        if (
            order.status != "NEW"
            or order.filled_quantity != 0
            or order.fees != 0
            or order.average_price != 0
        ):
            raise ValueError("New orders must be unfilled")
        last = self.last_bars.get(order.symbol)
        if last and parse_timestamp(order.timestamp) < parse_timestamp(last):
            raise ValueError("Backdated order")
        reserved = sum(
            o.remaining
            for o in self.orders.values()
            if o.symbol == order.symbol and o.side == "SELL" and o.status in ACTIVE_STATUSES
        )
        position = self.portfolio.positions.get(order.symbol)
        if order.side == "SELL" and (
            position is None or order.quantity > position.quantity - reserved + 1e-8
        ):
            order.status, order.reason = "REJECTED", "insufficient_position"
        self.orders[order.id] = order
        self._persist()
        logger.info(
            "paper_order",
            extra={
                "symbol": order.symbol,
                "order": order.id,
                "quantity": order.quantity,
                "strategy": order.strategy,
            },
        )
        return order.to_dict()

    def cancel_order(self, order_id):
        order = self.orders[order_id]
        if order.status not in ACTIVE_STATUSES:
            raise ValueError("Only active orders can be cancelled")
        order.status = "CANCELLED"
        self._persist()
        return order.to_dict()

    def get_order(self, order_id):
        return self.orders[order_id].to_dict()

    def get_orders(self):
        return [o.to_dict() for o in self.orders.values()]

    def get_positions(self):
        return [
            dict(
                asdict(p),
                mark=self.portfolio.marks.get(s, p.entry_price),
                unrealized_pnl=p.quantity
                * (self.portfolio.marks.get(s, p.entry_price) - p.entry_price)
                - p.entry_fees,
            )
            for s, p in self.portfolio.positions.items()
        ]

    def get_balance(self):
        timestamp = self.portfolio.last_timestamp or datetime.now(timezone.utc).isoformat()
        return self.portfolio.snapshot(timestamp, self.currency)

    def _fill(self, order, quantity, price, timestamp, reason="signal", reference=None):
        fee = self.config.fee(quantity, price)
        impact = quantity * abs(price - reference) if reference is not None else 0.0
        fill = Fill(
            order.id,
            order.symbol,
            order.side,
            quantity,
            price,
            fee,
            timestamp,
            order.strategy,
            reason,
            impact,
        )
        self.portfolio.apply_fill(fill)
        total = order.filled_quantity + quantity
        order.average_price = (
            order.average_price * order.filled_quantity + quantity * price
        ) / total
        order.filled_quantity, order.fees = total, order.fees + fee
        order.status = "FILLED" if order.remaining < 1e-8 else "PARTIALLY_FILLED"
        self.fills.append(fill)
        logger.debug(
            "paper_fill",
            extra={
                "symbol": order.symbol,
                "order": order.id,
                "price": price,
                "quantity": quantity,
                "strategy": order.strategy,
            },
        )

    def on_bar(self, candle, *, persist=True) -> dict | None:
        validate([candle], self.timeframe).require_valid()
        timestamp = candle.timestamp.isoformat()
        if candle.symbol in self.last_bars and candle.timestamp < parse_timestamp(
            self.last_bars[candle.symbol]
        ):
            raise ValueError("Out-of-order candle")
        if self.last_bars.get(candle.symbol) == timestamp:
            if (
                candle.symbol in self.last_candles
                and self.last_candles[candle.symbol] != candle.to_dict()
            ):
                raise ValueError("Conflicting duplicate candle")
            return None  # Reconnect/replay idempotency.
        if not self.calendar.is_open(candle.timestamp):
            return None
        exposed_at_open = bool(self.portfolio.positions)
        budget = candle.volume * self.config.max_participation
        bought = False
        for order in list(self.orders.values()):
            if order.status not in ACTIVE_STATUSES or order.symbol != candle.symbol:
                continue
            if parse_timestamp(order.timestamp) > candle.timestamp:
                continue
            price = execution_price(order, candle, self.config)
            if price is None or budget <= 1e-8:
                continue
            quantity = min(order.remaining, budget)
            if order.side == "BUY":
                affordable = self.portfolio.cash / (price * (1 + self.config.commission))
                quantity = min(quantity, affordable)
                if quantity <= 1e-8:
                    order.status, order.reason = "REJECTED", "insufficient_cash"
                    continue
            else:
                position = self.portfolio.positions.get(candle.symbol)
                if not position:
                    order.status, order.reason = "REJECTED", "insufficient_position"
                    continue
                quantity = min(quantity, position.quantity)
            reference = candle.open
            intrabar = False
            if order.order_type == "LIMIT":
                reference = (
                    min(candle.open, order.limit_price)
                    if order.side == "BUY"
                    else max(candle.open, order.limit_price)
                )
                intrabar = reference != candle.open
            fill_time = candle.available_at(self.timeframe).isoformat() if intrabar else timestamp
            self._fill(order, quantity, price, fill_time, order.reason or "signal", reference)
            budget -= quantity
            bought = bought or order.side == "BUY"
            if (
                order.side == "BUY"
                and order.remaining > 1e-8
                and self.portfolio.cash < price * (1 + self.config.commission) * 1e-8
            ):
                order.status, order.reason = "CANCELLED", "unaffordable_remainder"
        # Candle path is unknown. Stop wins if both levels hit. No entry-bar TP.
        position = self.portfolio.positions.get(candle.symbol)
        if position:
            stop = (
                position.entry_price * (1 - self.config.stop_loss)
                if self.config.stop_loss
                else None
            )
            take = (
                position.entry_price * (1 + self.config.take_profit)
                if self.config.take_profit
                else None
            )
            reason, reference = None, None
            if stop is not None and candle.low <= stop:
                reason, reference = "stop_loss", min(stop, candle.open)
            elif not bought and take is not None and candle.high >= take:
                reason, reference = (
                    "take_profit",
                    take,
                )  # Conservative, no favorable gap improvement.
            if reason:
                for order in self.orders.values():
                    if order.symbol == candle.symbol and order.status in ACTIVE_STATUSES:
                        order.status, order.reason = "CANCELLED", "replaced_by_risk_exit"
                order = Order(
                    candle.symbol,
                    "SELL",
                    position.quantity,
                    timestamp,
                    position.strategy,
                    reason=reason,
                )
                self.orders[order.id] = order
                quantity = min(position.quantity, budget)
                if quantity > 1e-8:
                    self._fill(
                        order,
                        quantity,
                        self.config.fill_price(reference, "SELL"),
                        candle.available_at(self.timeframe).isoformat(),
                        reason,
                        reference,
                    )
                # Any liquidity-limited remainder stays a market order for next bar.
        self.portfolio.marks[candle.symbol] = candle.close
        self.last_bars[candle.symbol] = timestamp
        self.last_candles[candle.symbol] = candle.to_dict()
        snapshot = self.portfolio.snapshot(
            candle.available_at(self.timeframe).isoformat(), self.currency
        )
        snapshot["exposed"] = snapshot["exposed"] or exposed_at_open or bought
        if persist:
            self._persist(snapshot)
        return snapshot

    def handle_signal(self, signal, symbol, timestamp, strategy, *, persist=True):
        """No pyramiding/leverage; size from current equity and available cash."""
        from app.strategies.base import Signal

        active = [
            o for o in self.orders.values() if o.symbol == symbol and o.status in ACTIVE_STATUSES
        ]
        if signal in {Signal.SELL, Signal.EXIT}:
            for order in active:
                if order.side == "BUY":
                    order.status, order.reason = "CANCELLED", "exit_signal"
            if any(o.side == "SELL" for o in active):
                return
            position = self.portfolio.positions.get(symbol)
            if position:
                order = Order(symbol, "SELL", position.quantity, timestamp, strategy)
                self.orders[order.id] = order
        elif signal == Signal.BUY and symbol not in self.portfolio.positions and not active:
            mark = self.portfolio.marks.get(symbol)
            if mark is None:
                raise ValueError("No market price for sizing")
            budget = min(self.portfolio.cash, self.portfolio.equity * self.config.position_size)
            quantity = budget / (self.config.fill_price(mark, "BUY") * (1 + self.config.commission))
            if quantity > 1e-8:
                order = Order(symbol, "BUY", quantity, timestamp, strategy)
                self.orders[order.id] = order
        if persist:
            self._persist()

    def cancel_all(self, *, persist=True):
        for order in self.orders.values():
            if order.status in ACTIVE_STATUSES:
                order.status, order.reason = "CANCELLED", "bot_stopped"
        if persist:
            self._persist()

    def close(self):
        if self.repository:
            self._persist()
            self.repository.release_writer()
