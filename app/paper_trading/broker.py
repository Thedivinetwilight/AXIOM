"""Paper is the only implemented broker. No SDK or real order URL exists."""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from math import isfinite
import logging
from app.backtesting.costs import ExecutionConfig
from app.backtesting.orders import ACTIVE_STATUSES, Fill, Order
from app.backtesting.portfolio import Portfolio
from app.data.models import parse_timestamp
from app.data.validator import validate
from app.paper_trading.execution import ContinuousCalendar, execution_price, calendar_signature
from app.paper_trading.compatibility import (
    PaperAccountCompatibilityError,
    differing_paper_fields,
)

logger = logging.getLogger("bot.broker")


class PaperRiskLimits:
    """Bounded paper risk controls, kept outside the execution-cost schema.

    Every limit is entry-side (or halt-side) only: exits are never blocked by a
    limit, because a risk control that can trap inventory is not a control.
    ``0`` disables a limit for legacy replay workflows; the US Alpaca paper mode
    requires all of them to be explicitly bounded before it will start.
    """

    def __init__(self, values=None):
        values = values or {}
        self.max_position_value = float(values.get("max_position_value", 0.0))
        self.max_open_positions = int(values.get("max_open_positions", 0))
        self.max_daily_loss = float(values.get("max_daily_loss", 0.0))
        self.max_drawdown = float(values.get("max_drawdown", 0.0))
        self.max_trades_per_day = int(values.get("max_trades_per_day", 0))
        self.max_gross_exposure = float(values.get("max_gross_exposure", 0.0))
        self.halt_policy = str(values.get("halt_policy", "BLOCK_ENTRIES")).upper()
        if any(not isfinite(v) or v < 0 for v in
               (self.max_position_value, self.max_daily_loss, self.max_gross_exposure)):
            raise ValueError("Paper monetary risk limits must be finite and non-negative")
        if any(v < 0 for v in (self.max_open_positions, self.max_trades_per_day)):
            raise ValueError("Paper count risk limits must be non-negative")
        if not isfinite(self.max_drawdown) or not 0 <= self.max_drawdown < 1:
            raise ValueError("Paper max_drawdown must be in [0, 1)")
        if not 0 <= self.max_gross_exposure <= 1:
            raise ValueError("Paper max_gross_exposure must be in [0, 1]")
        if self.halt_policy not in {"BLOCK_ENTRIES", "FLATTEN"}:
            raise ValueError("Paper halt_policy must be BLOCK_ENTRIES or FLATTEN")

    def to_dict(self):
        return {name: getattr(self, name) for name in (
            "max_position_value", "max_open_positions", "max_daily_loss",
            "max_drawdown", "max_trades_per_day", "max_gross_exposure",
            "halt_policy")}


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
        risk_limits: dict | None = None,
        repository=None,
        restore=True,
    ):
        from app.data.models import TIMEFRAMES

        if timeframe not in TIMEFRAMES:
            raise ValueError("Unsupported timeframe")
        self.config = config or ExecutionConfig()
        self.risk_limits = PaperRiskLimits(risk_limits)
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
                if state is not None:
                    # One shared, field-granular compatibility rule. A saved
                    # account carries the execution assumptions it was created
                    # under; restoring it under different assumptions is
                    # unsafe, so this refuses with the differing field NAMES
                    # and the database path — and never changes the account.
                    current_calendar = calendar_signature(self.calendar)
                    fields = differing_paper_fields(
                        state,
                        asdict(self.config),
                        currency=currency,
                        timeframe=timeframe,
                        calendar_signature=current_calendar,
                    )
                    if fields:
                        database = getattr(repository, "db", None)
                        database_path = getattr(database, "resolved_path", None)
                        if database_path is None:
                            database_path = getattr(database, "path", None)
                        raise PaperAccountCompatibilityError(fields, database_path)
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

    def _duplicate_entry(self, order) -> bool:
        """True when an identical strategy order already exists (restart replay)."""
        if not order.dedupe_key:
            return False
        if any(existing.dedupe_key == order.dedupe_key for existing in self.orders.values()):
            self.record_risk_event(order.timestamp, "order_deduplicated", {
                "symbol": order.symbol, "dedupe_key": order.dedupe_key
            })
            return True
        return False

    def record_risk_event(self, timestamp: str, kind: str, detail: dict | None = None):
        """Bounded, append-only paper risk log (never contains secrets)."""
        events = self.context.setdefault("risk_events", [])
        events.append({"timestamp": str(timestamp), "kind": kind, "detail": detail or {}})
        del events[:-200]  # keep the newest 200; the account file stays small

    def gross_exposure(self, *, include_pending: bool = True,
                       exclude=None) -> float:
        """Marked position value plus (optionally) resting BUY notional.

        Pending entries must count: three entry orders submitted in the same bar
        are three separate exposure commitments, and ignoring them let a single
        exposure cap be spent three times over before any fill happened.
        """
        total = sum(
            position.quantity * self.portfolio.marks.get(symbol, position.entry_price)
            for symbol, position in self.portfolio.positions.items()
        )
        if not include_pending:
            return total
        for order in self.orders.values():
            if order is exclude or order.side != "BUY" or order.status not in ACTIVE_STATUSES:
                continue
            mark = self.portfolio.marks.get(order.symbol)
            if mark is not None:
                total += order.remaining * mark
        return total

    def _update_paper_entry_halts(self, timestamp: str):
        from zoneinfo import ZoneInfo

        zone = getattr(self.calendar, "timezone", "UTC")
        day = parse_timestamp(timestamp).astimezone(ZoneInfo(zone)).date().isoformat()
        halts = self.context.setdefault("paper_risk_halts", {})
        equity, limits = self.portfolio.equity, self.risk_limits
        if limits.max_daily_loss > 0 and self.portfolio.day_start_equity - equity >= limits.max_daily_loss:
            if halts.get("daily_loss_date") != day:
                self.record_risk_event(timestamp, "daily_loss_halt", {
                    "day": day,
                    "daily_pnl": round(equity - self.portfolio.day_start_equity, 6),
                    "limit": limits.max_daily_loss,
                })
            halts["daily_loss_date"] = day
        if (limits.max_drawdown > 0 and self.portfolio.peak > 0
                and 1 - equity / self.portfolio.peak >= limits.max_drawdown):
            if not halts.get("drawdown"):
                self.record_risk_event(timestamp, "drawdown_halt", {
                    "drawdown": round(1 - equity / self.portfolio.peak, 6),
                    "limit": limits.max_drawdown,
                })
            halts["drawdown"] = True
        return day, halts.get("daily_loss_date") == day, bool(halts.get("drawdown"))

    def _apply_halt_policy(self, timestamp: str, halted: bool):
        """Cancel resting entries; FLATTEN also schedules simulated exits.

        Nothing here runs a strategy or reads future data: exits are ordinary
        market orders that fill from the next completed bar, exactly like any
        other paper order.
        """
        if not halted:
            return
        for order in self.orders.values():
            if order.side == "BUY" and order.status in ACTIVE_STATUSES:
                order.status, order.reason = "CANCELLED", "paper_risk_limit_breach"
        if self.risk_limits.halt_policy != "FLATTEN":
            return
        for symbol, position in list(self.portfolio.positions.items()):
            if any(
                o.symbol == symbol and o.side == "SELL" and o.status in ACTIVE_STATUSES
                for o in self.orders.values()
            ):
                continue
            exit_order = Order(
                symbol, "SELL", position.quantity, timestamp, position.strategy,
                reason="risk_halt_flatten",
            )
            self.orders[exit_order.id] = exit_order
            self.record_risk_event(timestamp, "flatten_exit_submitted", {"symbol": symbol})

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
            "risk_limits": self.risk_limits.to_dict(),
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
        # Roll the session-day boundary on last-known equity, then mark this
        # symbol at the new bar's open before deciding whether resting entries
        # remain allowed after a price gap.
        self.portfolio.snapshot(timestamp, self.currency)
        self.portfolio.marks[candle.symbol] = candle.open
        _, daily_halt, drawdown_halt = self._update_paper_entry_halts(timestamp)
        self._apply_halt_policy(timestamp, daily_halt or drawdown_halt)
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
                cap = self.risk_limits.max_position_value
                if cap > 0:
                    position = self.portfolio.positions.get(candle.symbol)
                    current_value = position.quantity * price if position else 0.0
                    quantity = min(quantity, max(0.0, cap - current_value) / price)
                    if quantity <= 1e-8:
                        order.status, order.reason = "CANCELLED", "paper_max_position_value"
                        continue
                exposure_cap = self.risk_limits.max_gross_exposure
                if exposure_cap > 0:
                    capacity = (exposure_cap * self.portfolio.equity
                                - self.gross_exposure(exclude=order))
                    quantity = min(quantity, max(0.0, capacity) / price)
                    if quantity <= 1e-8:
                        order.status, order.reason = "CANCELLED", "paper_max_gross_exposure"
                        continue
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
        """No pyramiding/leverage; size from current equity and available cash.

        Every strategy order carries a deterministic ``dedupe_key`` derived from
        (strategy, symbol, side, decision time). Re-delivering the same closed
        bar after a crash or a reconnect therefore cannot create a second
        simulated trade: the key already exists and the call is a no-op.
        """
        from app.strategies.base import Signal

        if signal not in {Signal.BUY, Signal.SELL, Signal.EXIT}:
            return
        side = "BUY" if signal == Signal.BUY else "SELL"
        dedupe_key = f"{strategy}|{symbol}|{side}|{timestamp}"
        if any(order.dedupe_key == dedupe_key for order in self.orders.values()):
            self.record_risk_event(timestamp, "order_deduplicated", {
                "symbol": symbol, "dedupe_key": dedupe_key
            })
            logger.info("paper_order_deduplicated", extra={"symbol": symbol})
            if persist:
                self._persist()
            return

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
                order = Order(symbol, "SELL", position.quantity, timestamp, strategy,
                              reason="exit_signal", dedupe_key=dedupe_key)
                self.orders[order.id] = order
        elif signal == Signal.BUY and symbol not in self.portfolio.positions and not active:
            mark = self.portfolio.marks.get(symbol)
            if mark is None:
                raise ValueError("No market price for sizing")
            limits = self.risk_limits
            trade_day, daily_halt, drawdown_halt = self._update_paper_entry_halts(timestamp)
            from zoneinfo import ZoneInfo
            calendar_zone = getattr(self.calendar, "timezone", "UTC")
            entry_count = sum(
                1 for order in self.orders.values()
                if order.side == "BUY"
                and parse_timestamp(order.timestamp).astimezone(
                    ZoneInfo(calendar_zone)).date().isoformat() == trade_day
            )
            open_or_pending = len(self.portfolio.positions) + sum(
                1 for order in self.orders.values()
                if order.side == "BUY" and order.status in ACTIVE_STATUSES
                and order.symbol not in self.portfolio.positions
            )
            block_reason = None
            if daily_halt:
                block_reason = "paper_max_daily_loss"
            elif drawdown_halt:
                block_reason = "paper_max_drawdown"
            elif limits.max_open_positions and open_or_pending >= limits.max_open_positions:
                block_reason = "paper_max_open_positions"
            elif limits.max_trades_per_day and entry_count >= limits.max_trades_per_day:
                block_reason = "paper_max_trades_per_day"
            if block_reason:
                logger.warning("paper_entry_blocked",
                               extra={"symbol": symbol, "reason": block_reason})
                self.record_risk_event(timestamp, "entry_blocked", {
                    "symbol": symbol, "reason": block_reason
                })
                if persist:
                    self._persist()
                return
            budget = min(self.portfolio.cash, self.portfolio.equity * self.config.position_size)
            if limits.max_position_value > 0:
                budget = min(budget, limits.max_position_value)
            level = self.config.fill_price(mark, "BUY") * (1 + self.config.commission)
            if limits.max_gross_exposure > 0:
                capacity = (limits.max_gross_exposure * self.portfolio.equity
                            - self.gross_exposure())
                # A residual below 1% of the exposure budget is never a real
                # allocation; treating it as room emits dust orders that each
                # re-take the cap and quietly multiply turnover.
                if capacity < max(1.0, 0.01 * limits.max_gross_exposure
                                  * self.portfolio.equity):
                    logger.warning("paper_entry_blocked",
                                   extra={"symbol": symbol,
                                          "reason": "paper_max_gross_exposure"})
                    self.record_risk_event(timestamp, "entry_blocked", {
                        "symbol": symbol, "reason": "paper_max_gross_exposure"
                    })
                    if persist:
                        self._persist()
                    return
                budget = min(budget, capacity)
            quantity = budget / level
            if quantity > 1e-8:
                order = Order(symbol, "BUY", quantity, timestamp, strategy,
                              dedupe_key=dedupe_key)
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
