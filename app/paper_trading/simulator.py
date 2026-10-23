"""Async lifecycle. Pause inhibits strategy orders, not protective risk handling."""

import asyncio
from enum import Enum
import logging
import time
from datetime import datetime, timezone, timedelta
from app.data.models import Candle, parse_timestamp
from app.data.realtime import ReplayStream
from app.data.validator import validate
from app.strategies.base import Signal

logger = logging.getLogger("bot.controller")


class BotState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class PaperSimulator:
    def __init__(self, broker):
        self.broker = broker
        self.state = BotState.STOPPED
        self.provider = None
        self.strategy = None
        self.task: asyncio.Task | None = None
        self.error: str | None = None
        self.signals: dict = {}
        self.processed = 0
        self.symbols: list[str] = []
        self._history: dict[str, list[Candle]] = {}
        # Chart buffer: the bars this process actually processed, for the
        # trading terminal. Kept in memory on purpose — the paper account row
        # must not grow by a bar per minute, and a chart that silently lost
        # its history would be worse than one that says so.
        self.chart_bars: dict[str, list[Candle]] = {}
        self.chart_limit = 1500
        # V0.6.1: wall-clock processing latency stages (rolling aggregates only).
        self.latency = {
            "bars": 0,
            "avg_total_ms": 0.0,
            "max_total_ms": 0.0,
            "avg_strategy_ms": 0.0,
            "avg_order_ms": 0.0,
        }
        # V0.6.2: risk-decision counters for live pipeline observability.
        self.risk_counters = {"actionable_signals": 0, "orders_created": 0, "skipped": 0}

    def status(self):
        status = {
            "mode": "PAPER",
            "live_trading": False,
            "state": self.state.value,
            "connection": self.provider.status if self.provider else "DISCONNECTED",
            "last_message": self.provider.last_message if self.provider else None,
            "strategy": self.strategy.name if self.strategy else None,
            "parameters": self.strategy.parameters if self.strategy else None,
            "symbols": self.symbols,
            "signals": self.signals,
            "processed_bars": self.processed,
            "error": self.error,
        }
        # V0.6.1: live data adapters expose health/monitoring fields; secrets are
        # never included (see UpstoxStream.metrics). Existing keys are unchanged.
        status["paper_mode"] = True  # execution is simulated, whatever the feed
        get_metrics = getattr(self.provider, "metrics", None)
        if get_metrics:
            feed = get_metrics()
            for key in (
                "connection_status",
                "authenticated",
                "subscribed",
                "last_message_time",
                "last_candle_time",
                "data_gap_detected",
                "stale_feed",
                "paper_mode",
                "reconnects",
                "feed_counters",
                "feed_latency_ms",
                "connect_latency_ms",
                "market_status",
                "market_open",
                "segment_statuses",
                "last_tick",
                "suppressed_by_market_status",
                "provider",
                "feed_mode",
                "timeframe",
                "instrument_keys",
            ):
                if key in feed:
                    status[key] = feed[key]
        status["processing_latency_ms"] = dict(self.latency)
        status["risk_counters"] = dict(self.risk_counters)
        if hasattr(self.broker, "risk_limits"):
            status["paper_risk_limits"] = self.broker.risk_limits.to_dict()
            status["paper_risk_halts"] = dict(
                self.broker.context.get("paper_risk_halts", {})
            )
        return status

    async def start(self, provider, strategy, symbols):
        if self.task and not self.task.done():
            raise ValueError("Bot already active")
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("Specify distinct symbols")
        from app.data.models import validate_symbol

        for symbol in symbols:
            validate_symbol(symbol)
        config = {
            "strategy": strategy.name,
            "parameters": strategy.parameters,
            "symbols": symbols,
            "provider": type(provider).__name__,
        }
        old = self.broker.context.get("selection")
        if (
            old
            and old != config
            and (
                self.broker.portfolio.positions
                or any(o["status"] in {"NEW", "PARTIALLY_FILLED"} for o in self.broker.get_orders())
            )
        ):
            raise ValueError(
                "Cannot switch selection/source with inventory or active orders; use a separate account DB"
            )
        self._history = (
            {
                s: [Candle.from_dict(b) for b in bars]
                for s, bars in self.broker.context.get("history", {}).items()
            }
            if old == config
            else {}
        )
        self.broker.context["selection"] = config
        self.provider, self.strategy, self.symbols = provider, strategy, symbols
        self.error, self.state = None, BotState.STARTING
        self.task = asyncio.create_task(self._run())
        return self.status()

    def process_bar(self, candle):
        validate([candle], self.broker.timeframe).require_valid()
        if not isinstance(self.provider, ReplayStream) and candle.available_at(
            self.broker.timeframe
        ) > datetime.now(timezone.utc) + timedelta(seconds=2):
            raise ValueError("Live feed sent an unfinished/future candle")
        stale_after = getattr(self.provider, "stale_after_seconds", None)
        if stale_after is not None and (
            datetime.now(timezone.utc) - candle.available_at(self.broker.timeframe)
        ).total_seconds() > stale_after:
            raise ValueError("Live feed sent a stale candle")
        last = self.broker.last_bars.get(candle.symbol)
        if last and candle.timestamp <= parse_timestamp(last):
            if candle.timestamp == parse_timestamp(last):
                prior = self.broker.last_candles.get(candle.symbol)
                if prior and prior != candle.to_dict():
                    raise ValueError("Conflicting duplicate candle")
                return  # duplicate closed-candle reconnect event
            raise ValueError("Out-of-order live candle")
        history = self._history.setdefault(candle.symbol, [])
        if history:
            report = validate(
                [history[-1], candle], self.broker.timeframe, calendar=self.broker.calendar
            )
            report.require_valid()
            if report.warnings:
                self.state = BotState.PAUSED
                self.broker.cancel_all(persist=False)
                history.clear()
                self.error = (
                    "Data gap: orders cancelled, new signals paused; backfill/review before resume"
                )
                logger.warning("data_gap_paused", extra={"symbol": candle.symbol})
        started = time.perf_counter()
        snapshot = self.broker.on_bar(candle, persist=False)
        if snapshot is None:
            return
        history.append(candle)
        history[:] = history[-self.strategy.history_limit :]
        drawn = self.chart_bars.setdefault(candle.symbol, [])
        drawn.append(candle)
        del drawn[: -self.chart_limit]
        evaluated = time.perf_counter()
        signal = (
            self.strategy.generate_signal(tuple(history))
            if self.state == BotState.RUNNING
            else Signal.HOLD
        )
        self.signals[candle.symbol] = {"signal": signal.value, "timestamp": snapshot["timestamp"]}
        logger.debug(
            "CANDLE_COMPLETED",
            extra={"symbol": candle.symbol, "timestamp": candle.timestamp.isoformat()},
        )
        orders_before = len(self.broker.orders)
        fills_before = len(self.broker.fills)
        self.broker.handle_signal(
            signal, candle.symbol, snapshot["timestamp"], self.strategy.name, persist=False
        )
        created = len(self.broker.orders) - orders_before
        new_fills = len(self.broker.fills) - fills_before
        if signal != Signal.HOLD:
            # V0.6.2 structured pipeline events (paper-only; never any order routing).
            logger.info(
                "STRATEGY_SIGNAL",
                extra={"symbol": candle.symbol, "signal": signal.value,
                       "strategy": self.strategy.name},
            )
            self.risk_counters["actionable_signals"] += 1
            self.risk_counters["orders_created"] += created
            if created:
                logger.info("RISK_DECISION", extra={"decision": "order_created",
                                                    "symbol": candle.symbol})
            else:
                self.risk_counters["skipped"] += 1
                logger.info(
                    "RISK_DECISION",
                    extra={"decision": "skipped_no_order", "symbol": candle.symbol},
                )
                logger.info(
                    "PAPER_REJECT",
                    extra={"symbol": candle.symbol, "reason": "risk_skip_no_new_order"},
                )
        for order in list(self.broker.orders.values())[orders_before:]:
            logger.info(
                "PAPER_ORDER",
                extra={"symbol": order.symbol, "side": order.side,
                       "quantity": order.quantity, "status": order.status},
            )
        if new_fills:
            logger.info("PAPER_FILL", extra={"symbol": candle.symbol, "fills": new_fills})
        # V0.6.1: rolling processing-latency stages; aggregates only, no growth.
        finished = time.perf_counter()
        bars = self.latency["bars"] + 1
        self.latency["bars"] = bars
        total_ms = (finished - started) * 1000
        strategy_ms = (evaluated - started) * 1000
        order_ms = (finished - evaluated) * 1000
        self.latency["avg_total_ms"] = round(
            self.latency["avg_total_ms"] + (total_ms - self.latency["avg_total_ms"]) / bars, 3
        )
        self.latency["max_total_ms"] = max(self.latency["max_total_ms"], round(total_ms, 3))
        self.latency["avg_strategy_ms"] = round(
            self.latency["avg_strategy_ms"] + (strategy_ms - self.latency["avg_strategy_ms"]) / bars,
            3,
        )
        self.latency["avg_order_ms"] = round(
            self.latency["avg_order_ms"] + (order_ms - self.latency["avg_order_ms"]) / bars, 3
        )
        self.broker.context["history"] = {
            s: [b.to_dict() for b in bars] for s, bars in self._history.items()
        }
        # One SQLite transaction for fills, account, dedupe cursor and signal orders.
        self.broker._persist(snapshot)
        self.processed += 1
        logger.debug(
            "signal",
            extra={
                "symbol": candle.symbol,
                "strategy": self.strategy.name,
                "signal": signal.value,
                "price": candle.close,
            },
        )

    async def _run(self):
        self.state = BotState.RUNNING
        try:
            async for candle in self.provider.stream(self.symbols, self.broker.timeframe):
                # Replay resumes after the committed cursor. Live out-of-order data fails closed.
                last = self.broker.last_bars.get(candle.symbol)
                if (
                    isinstance(self.provider, ReplayStream)
                    and last
                    and candle.timestamp < parse_timestamp(last)
                ):
                    continue
                self.process_bar(candle)
        except asyncio.CancelledError:
            pass  # documented: loop exits on task cancellation
        except Exception as exc:
            self.state = BotState.ERROR
            self.error = type(exc).__name__  # Never echo provider URLs/auth payloads.
            logger.error("paper_failed", extra={"error": type(exc).__name__})
        finally:
            self.broker.cancel_all()
            await self.provider.close()
            if self.state != BotState.ERROR:
                self.state = BotState.STOPPED
            logger.info("paper_stopped")

    async def pause(self):
        if self.state != BotState.RUNNING:
            raise ValueError("Can only pause a running bot")
        self.state = BotState.PAUSED
        # Cancel pending entries; exits/risk continue with new closed bars.
        for order in self.broker.get_orders():
            if order["side"] == "BUY" and order["status"] in {"NEW", "PARTIALLY_FILLED"}:
                self.broker.cancel_order(order["id"])
        return self.status()

    async def resume(self):
        if self.state != BotState.PAUSED:
            raise ValueError("Can only resume a paused bot")
        self.state, self.error = BotState.RUNNING, None
        return self.status()

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await self.task
        self.broker.cancel_all()
        self.state = BotState.STOPPED
        return self.status()
