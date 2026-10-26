"""Async lifecycle. Pause inhibits strategy orders, not protective risk handling."""

import asyncio
from enum import Enum
import logging
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from app.data.models import Candle, parse_timestamp
from app.data.realtime import ReplayStream
from app.data.validator import validate
from app.strategies.base import Signal

logger = logging.getLogger("bot.controller")

# Audit payloads are allowlisted, scalar and provider-independent. In particular,
# never serialize provider.__dict__, request settings, exception text or feed URLs:
# those can contain access tokens, passwords or account-specific secret material.
_AUDIT_FIELDS = {
    "provider", "feed_kind", "strategy", "review_policy", "automation_enabled",
    "symbol", "symbols", "timeframe", "state", "stop_reason", "error_type",
    "signal", "market_timestamp", "order_id", "fill_id", "side", "quantity",
    "price", "fee", "status", "reason", "processed_bars", "actionable_signals",
    "orders_created", "fills", "realized_pnl", "unrealized_pnl_delta",
    "ending_unrealized_pnl", "equity_delta",
}


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
        # A run is process-local. Its final summary is persisted separately from
        # account-wide P&L, which deliberately survives process restart.
        self.stop_reason: str | None = None
        self.run: dict | None = None
        self.last_run: dict | None = (
            dict(broker.context["last_paper_run"])
            if isinstance(broker.context.get("last_paper_run"), dict) else None
        )
        self._event_sequence = 0

    def _automation_enabled(self) -> bool:
        return bool(self.strategy and self.strategy.name != "manual_only")

    def _review_policy(self) -> str:
        if not self.strategy:
            return "NO_ACTIVE_STRATEGY"
        return (
            "MANUAL_ORDER_REVIEW" if self.strategy.name == "manual_only"
            else "AUTOMATED_LOCAL_SIMULATION"
        )

    def _feed_kind(self) -> str | None:
        if not self.provider:
            return None
        if isinstance(self.provider, ReplayStream):
            return "LOCAL_REPLAY"
        if getattr(self.provider, "requires_fresh_live", False):
            return "MT5_READ_ONLY_LOCAL_PAPER"
        return "READ_ONLY_STREAM_LOCAL_PAPER"

    def _run_view(self) -> dict | None:
        if not self.run:
            return None
        realized = float(self.broker.portfolio.realized_pnl)
        unrealized = float(self.broker.portfolio.unrealized_pnl)
        return {
            "run_id": self.run["run_id"],
            "state": self.state.value,
            "active": self.state in {BotState.STARTING, BotState.RUNNING, BotState.PAUSED},
            "provider": type(self.provider).__name__ if self.provider else None,
            "feed_kind": self._feed_kind(),
            "strategy": self.strategy.name if self.strategy else None,
            "automation_enabled": self._automation_enabled(),
            "review_policy": self._review_policy(),
            "started_at": self.run["started_at"],
            "stopped_at": self.run.get("stopped_at"),
            "stop_reason": self.stop_reason,
            "processed_bars": self.processed,
            "actionable_signals": self.run["actionable_signals"],
            "orders_created": self.run["orders_created"],
            "fills": self.run["fills"],
            "realized_pnl": realized - self.run["realized_baseline"],
            "unrealized_pnl_delta": unrealized - self.run["unrealized_baseline"],
            "ending_unrealized_pnl": unrealized,
            "equity_delta": float(self.broker.portfolio.equity) - self.run["equity_baseline"],
            "pnl_basis": "LOCAL_SIMULATED_CURRENT_RUN",
        }

    def _audit(self, kind: str, **payload):
        """Append one durable, redacted event for the current local-paper run."""
        repository = getattr(self.broker, "repository", None)
        if repository is None or not self.run:
            return
        self._event_sequence += 1
        safe = {}
        for key, value in payload.items():
            if key not in _AUDIT_FIELDS:
                continue
            if key == "symbols" and isinstance(value, (list, tuple)):
                safe[key] = [str(item)[:40] for item in value[:50]]
            elif value is None or isinstance(value, (str, int, float, bool)):
                safe[key] = value
        repository.record_system_event(kind, {
            "run_id": self.run["run_id"],
            "sequence": self._event_sequence,
            "simulated": True,
            "execution_mode": "AXIOM_PAPER",
            **safe,
        })

    def _audit_new_execution(self, orders_before: int, fills_before: int):
        """Journal new local orders/fills exactly once and update run counters."""
        orders = list(self.broker.orders.values())[orders_before:]
        fills = self.broker.fills[fills_before:]
        for order in orders:
            self._audit(
                "PAPER_ORDER_SIMULATED",
                order_id=order.id, symbol=order.symbol, side=order.side,
                quantity=order.quantity, price=order.average_price or order.limit_price,
                status=order.status, reason=order.reason, strategy=order.strategy,
                market_timestamp=order.timestamp,
            )
        for fill in fills:
            self._audit(
                "PAPER_FILL_SIMULATED",
                fill_id=fill.id, order_id=fill.order_id, symbol=fill.symbol,
                side=fill.side, quantity=fill.quantity, price=fill.price,
                fee=fill.fee, status="FILLED", reason=fill.reason,
                strategy=fill.strategy, market_timestamp=fill.timestamp,
            )
        if self.run:
            self.run["orders_created"] += len(orders)
            self.run["fills"] += len(fills)
        return len(orders), len(fills)

    def status(self):
        status = {
            "mode": "PAPER",
            "live_trading": False,
            "state": self.state.value,
            "connection": self.provider.status if self.provider else "DISCONNECTED",
            "last_message": self.provider.last_message if self.provider else None,
            "strategy": self.strategy.name if self.strategy else None,
            "parameters": self.strategy.parameters if self.strategy else None,
            "timeframe": self.broker.timeframe,
            "symbols": self.symbols,
            "signals": self.signals,
            "processed_bars": self.processed,
            "error": self.error,
            "provider": type(self.provider).__name__ if self.provider else None,
            "feed_kind": self._feed_kind(),
            "automation_enabled": self._automation_enabled(),
            "review_policy": self._review_policy(),
            "stop_reason": self.stop_reason,
            "current_run": self._run_view(),
            "last_run": dict(self.last_run) if self.last_run else None,
            "audit_enabled": getattr(self.broker, "repository", None) is not None,
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
        if getattr(provider, "requires_fresh_live", False):
            if strategy.name != "manual_only":
                raise ValueError("Fresh MT5 market data is manual-only; strategy automation stays OFF")
            if getattr(provider, "status", None) not in {"CONNECTED", "STREAMING"}:
                raise ValueError("Prepare authenticated fresh advancing MT5 data before starting paper")
        market_data_context = {
            "provider": getattr(provider, "provider_name", type(provider).__name__),
            "requires_fresh_live": bool(getattr(provider, "requires_fresh_live", False)),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "automation_enabled": strategy.name != "manual_only",
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
        missing = object()
        previous_selection = self.broker.context.get("selection", missing)
        previous_market_data = self.broker.context.get("market_data", missing)
        previous = (
            self.provider, self.strategy, self.symbols, self.state, self.error,
            self._history, self.processed, dict(self.signals), dict(self.risk_counters),
            dict(self.latency), self.run, self.stop_reason, self._event_sequence, self.task,
        )
        new_task = None
        try:
            self.broker.context["selection"] = config
            self.broker.context["market_data"] = market_data_context
            self.provider, self.strategy, self.symbols = provider, strategy, symbols
            self.error, self.state = None, BotState.STARTING
            self.stop_reason = None
            self.processed = 0
            self.signals = {}
            self.risk_counters = {"actionable_signals": 0, "orders_created": 0, "skipped": 0}
            self.latency = {
                "bars": 0, "avg_total_ms": 0.0, "max_total_ms": 0.0,
                "avg_strategy_ms": 0.0, "avg_order_ms": 0.0,
            }
            self._event_sequence = 0
            self.run = {
                "run_id": uuid4().hex,
                "started_at": market_data_context["started_at"],
                "realized_baseline": float(self.broker.portfolio.realized_pnl),
                "unrealized_baseline": float(self.broker.portfolio.unrealized_pnl),
                "equity_baseline": float(self.broker.portfolio.equity),
                "actionable_signals": 0,
                "orders_created": 0,
                "fills": 0,
            }
            result = self.status()
            new_task = asyncio.create_task(self._run())
            self.task = new_task
            self._audit(
                "PAPER_SESSION_STARTED",
                provider=type(provider).__name__, feed_kind=self._feed_kind(),
                strategy=strategy.name, review_policy=self._review_policy(),
                automation_enabled=self._automation_enabled(), symbols=symbols,
                timeframe=self.broker.timeframe, state=self.state.value,
            )
        except Exception:
            if new_task is not None:
                new_task.cancel()
            for key, value in (
                ("selection", previous_selection), ("market_data", previous_market_data),
            ):
                if value is missing:
                    self.broker.context.pop(key, None)
                else:
                    self.broker.context[key] = value
            (
                self.provider, self.strategy, self.symbols, self.state, self.error,
                self._history, self.processed, self.signals, self.risk_counters,
                self.latency, self.run, self.stop_reason, self._event_sequence, self.task,
            ) = previous
            raise
        return result

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
        get_provenance = getattr(self.provider, "provenance_for", None)
        provenance = get_provenance(candle) if callable(get_provenance) else None
        if getattr(self.provider, "requires_fresh_live", False) and not provenance:
            raise ValueError("Fresh live bar is missing durable quote provenance")
        execution_orders_before = len(self.broker.orders)
        execution_fills_before = len(self.broker.fills)
        snapshot = self.broker.on_bar(candle, persist=False, provenance=provenance)
        if snapshot is None:
            return
        # Resting entries and protective exits fill before this bar's new
        # strategy decision. Journal that execution before the next signal.
        self._audit_new_execution(execution_orders_before, execution_fills_before)
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
        if provenance:
            decisions = self.broker.context.setdefault("decision_provenance", {})
            decisions[candle.symbol] = {
                "signal": signal.value,
                "decision_timestamp": snapshot["timestamp"],
                **provenance,
            }
            while len(decisions) > 64:
                decisions.pop(next(iter(decisions)))
        logger.debug(
            "CANDLE_COMPLETED",
            extra={"symbol": candle.symbol, "timestamp": candle.timestamp.isoformat()},
        )
        orders_before = len(self.broker.orders)
        fills_before = len(self.broker.fills)
        if signal != Signal.HOLD:
            self._audit(
                "PAPER_STRATEGY_SIGNAL",
                symbol=candle.symbol, signal=signal.value, strategy=self.strategy.name,
                market_timestamp=snapshot["timestamp"],
            )
        self.broker.handle_signal(
            signal, candle.symbol, snapshot["timestamp"], self.strategy.name, persist=False
        )
        created, new_fills = self._audit_new_execution(orders_before, fills_before)
        if signal != Signal.HOLD:
            if self.run:
                self.run["actionable_signals"] += 1
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

    async def _finish_run(self):
        """Idempotent cleanup/final audit, including cancellation before first run."""
        if self.run and self.run.get("stopped_at"):
            return
        self.broker.cancel_all()
        if self.provider:
            await self.provider.close()
        if self.state != BotState.ERROR:
            self.state = BotState.STOPPED
        if self.run:
            self.run["stopped_at"] = datetime.now(timezone.utc).isoformat()
            summary = self._run_view()
            self.last_run = summary
            # Keep a completed run summary for restart display, but never
            # restore it as an active run or as account-wide current P&L.
            self.broker.context["last_paper_run"] = summary
            self.broker._persist()
            self._audit(
                "PAPER_SESSION_STOPPED",
                state=summary["state"], stop_reason=self.stop_reason,
                error_type=self.error, feed_kind=summary["feed_kind"],
                strategy=summary["strategy"], processed_bars=summary["processed_bars"],
                actionable_signals=summary["actionable_signals"],
                orders_created=summary["orders_created"], fills=summary["fills"],
                realized_pnl=summary["realized_pnl"],
                unrealized_pnl_delta=summary["unrealized_pnl_delta"],
                ending_unrealized_pnl=summary["ending_unrealized_pnl"],
                equity_delta=summary["equity_delta"],
            )

    async def _run(self):
        self.state = BotState.RUNNING
        self._audit(
            "PAPER_SESSION_RUNNING", state=self.state.value,
            feed_kind=self._feed_kind(), strategy=self.strategy.name,
            automation_enabled=self._automation_enabled(),
        )
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
            if self.stop_reason is None:
                self.stop_reason = "CANCELLED"
        except Exception as exc:
            self.state = BotState.ERROR
            self.stop_reason = "ERROR"
            self.error = type(exc).__name__  # Never echo provider URLs/auth payloads.
            logger.error("paper_failed", extra={"error": type(exc).__name__})
        finally:
            if self.state != BotState.ERROR and self.stop_reason is None:
                self.stop_reason = (
                    "END_OF_DATA" if isinstance(self.provider, ReplayStream)
                    else "STREAM_ENDED"
                )
            await self._finish_run()
            logger.info("paper_stopped", extra={"stop_reason": self.stop_reason})

    async def pause(self):
        if self.state != BotState.RUNNING:
            raise ValueError("Can only pause a running bot")
        self.state = BotState.PAUSED
        # Cancel pending entries; exits/risk continue with new closed bars.
        for order in self.broker.get_orders():
            if order["side"] == "BUY" and order["status"] in {"NEW", "PARTIALLY_FILLED"}:
                self.broker.cancel_order(order["id"])
        self._audit(
            "PAPER_SESSION_PAUSED", state=self.state.value,
            processed_bars=self.processed, strategy=self.strategy.name,
        )
        return self.status()

    async def resume(self):
        if self.state != BotState.PAUSED:
            raise ValueError("Can only resume a paused bot")
        readiness = getattr(self.provider, "assert_resume_ready", None)
        if callable(readiness):
            readiness()
        self.state, self.error = BotState.RUNNING, None
        self._audit(
            "PAPER_SESSION_RESUMED", state=self.state.value,
            processed_bars=self.processed, strategy=self.strategy.name,
        )
        return self.status()

    async def stop(self, reason: str = "OPERATOR_STOP"):
        if self.task and not self.task.done():
            if self.stop_reason is None:
                self.stop_reason = reason
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                # Cancellation before the coroutine's first step is delivered
                # to the awaiter; _finish_run below still journals the stop.
                pass
        if self.run and not self.run.get("stopped_at"):
            # A task cancelled before its first _run step has no finally block.
            if self.stop_reason is None:
                self.stop_reason = reason
            await self._finish_run()
        else:
            self.broker.cancel_all()
            if self.state != BotState.ERROR:
                self.state = BotState.STOPPED
        return self.status()
