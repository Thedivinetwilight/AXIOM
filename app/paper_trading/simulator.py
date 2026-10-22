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


def _session_state(calendar) -> tuple[str, str | None]:
    """OPEN / CLOSED / UNKNOWN for a calendar, never raising.

    A simulator that cannot tell the time must say so: an unknown session
    state is not the same thing as an open session.
    """
    is_open = getattr(calendar, "is_open", None)
    if not callable(is_open):
        return "UNKNOWN", "calendar exposes no session clock"
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        return ("OPEN" if is_open(_dt.now(_tz.utc)) else "CLOSED"), None
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return "UNKNOWN", f"{type(exc).__name__}: {exc}"


class PaperSimulator:
    def __init__(self, broker):
        self.broker = broker
        self.state = BotState.STOPPED
        self.provider = None
        self.strategy = None
        self.task: asyncio.Task | None = None
        self.error: str | None = None
        self.error_phase: str | None = None
        self.error_reason: str | None = None
        # set by stop(); makes shutdown idempotent, which matters because
        # Ctrl+C reaches both the signal handler and the shutdown path.
        self._stopped = False
        self.signals: dict = {}
        self.processed = 0
        self.symbols: list[str] = []
        self._history: dict[str, list[Candle]] = {}
        # AXIOM-MONEY-PRINTER research-telemetry hook (None = unchanged).
        self.workstation = None
        # V0.6.1: wall-clock processing latency stages (rolling aggregates only).
        self.latency = {
            "bars": 0,
            "avg_total_ms": 0.0,
            "max_total_ms": 0.0,
            "avg_strategy_ms": 0.0,
            "avg_order_ms": 0.0,
        }
        # V0.6.2: risk-decision counters for live pipeline observability.
        self.risk_counters = {
            "actionable_signals": 0,
            "orders_created": 0,
            "skipped": 0,
            # Feed-hygiene counters: every one of these is a bar the engine
            # refused or ignored, surfaced so a silent feed problem is visible.
            "rejected_bars": 0,
            "out_of_session_bars": 0,
            "deduplicated_bars": 0,
        }

    def _record_failure(self, exc: Exception, phase: str) -> None:
        """Expose only allowlisted diagnostics; never persist raw exception text."""
        error_type = type(exc).__name__
        message = str(exc)
        if error_type == "UpstoxAuthError":
            reason = "provider_authentication_failed"
        elif message in {
            "Alpaca stream rejected authentication/subscription",
            "Alpaca stream authentication failed",
            "Alpaca stream subscription failed",
            "Alpaca did not confirm authentication",
            "Alpaca did not confirm the bar subscription",
        }:
            reason = "provider_authentication_failed"
        elif message == "Alpaca stream failed repeatedly":
            reason = "provider_stream_failed"
        elif message == "Alpaca sent malformed stream data":
            reason = "malformed_stream_data"
        elif message == "Stream timeframe mismatch":
            reason = "provider_timeframe_mismatch"
        elif message == "Unsubscribed stream symbol":
            reason = "provider_symbol_mismatch"
        elif message == "Live feed sent an unfinished/future candle":
            reason = "unfinished_live_candle"
        elif message == "Out-of-order live candle":
            reason = "out_of_order_live_candle"
        elif message == "Live feed sent a stale candle":
            reason = "stale_live_candle"
        elif message == "Conflicting duplicate candle":
            reason = "conflicting_duplicate_candle"
        elif phase == "provider_stream":
            reason = "provider_stream_failed"
        elif phase in {"broker_cleanup", "provider_cleanup"}:
            reason = "paper_cleanup_failed"
        else:
            reason = "bar_processing_failed"

        self.state = BotState.ERROR
        self.error = error_type
        self.error_phase = phase
        self.error_reason = reason
        logger.error("paper_failed", extra={
            "error": error_type,
            "phase": phase,
            "reason": reason,
        })

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
            "error_phase": self.error_phase,
            "error_reason": self.error_reason,
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
                "stale_reason",
                "warmup",
                "paper_mode",
                "paper_only",
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
            status["paper_risk_events"] = list(
                self.broker.context.get("risk_events", [])
            )[-10:]
            status["gross_exposure"] = round(self.broker.gross_exposure(), 6)
            status["paper_account_equity"] = round(self.broker.portfolio.equity, 6)
        status["simulated_fills"] = True
        status["paper_only"] = True
        status["broker"] = "LOCAL_PAPER"
        status["session_timezone"] = getattr(
            self.broker.calendar, "timezone", "UTC"
        )
        # Session state belongs to the calendar, not to the feed: an operator
        # must be able to see "the market is closed" on a STOPPED engine
        # before starting one, instead of reading an empty status and
        # guessing. Missing data is still reported as missing, never as live.
        if "market_status" not in status:
            open_now, reason = _session_state(self.broker.calendar)
            status["market_status"] = open_now
            status["market_open"] = None if open_now == "UNKNOWN" else open_now == "OPEN"
            status["market_status_detail"] = reason
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
        self.error = self.error_phase = self.error_reason = None
        self.state = BotState.STARTING
        self.task = asyncio.create_task(self._run())
        return self.status()

    def process_bar(self, candle):
        validate([candle], self.broker.timeframe).require_valid()
        is_live = not isinstance(self.provider, ReplayStream)
        if is_live:
            now = datetime.now(timezone.utc)
            available_at = candle.available_at(self.broker.timeframe)
            if available_at > now + timedelta(seconds=2):
                self.risk_counters["rejected_bars"] += 1
                raise ValueError("Live feed sent an unfinished/future candle")
            if not self.broker.calendar.is_open(candle.timestamp):
                # Pre/post-market or out-of-session bar. Dropping it is the safe
                # action (the paper account only trades the regular session);
                # crashing the session on it would be a denial-of-service by
                # feed composition. The counter keeps it visible.
                self.risk_counters["out_of_session_bars"] += 1
                return
            stale_after = getattr(self.provider, "stale_after_seconds", None)
            if stale_after is not None and (
                now - available_at
            ).total_seconds() > stale_after:
                self.risk_counters["rejected_bars"] += 1
                raise ValueError("Live feed sent a stale candle")
        last = self.broker.last_bars.get(candle.symbol)
        if last and candle.timestamp <= parse_timestamp(last):
            if candle.timestamp == parse_timestamp(last):
                prior = self.broker.last_candles.get(candle.symbol)
                if prior and prior != candle.to_dict():
                    self.risk_counters["rejected_bars"] += 1
                    raise ValueError("Conflicting duplicate candle")
                self.risk_counters["deduplicated_bars"] += 1
                return  # duplicate closed-candle reconnect event
            self.risk_counters["rejected_bars"] += 1
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
        self._observe_workstation(candle)
        logger.debug(
            "signal",
            extra={
                "symbol": candle.symbol,
                "strategy": self.strategy.name,
                "signal": signal.value,
                "price": candle.close,
            },
        )

    def _observe_workstation(self, candle) -> None:
        """Feed observed bar facts into the intelligence layer (telemetry).

        Never part of the execution path: any failure is logged and ignored
        so research observation cannot disturb paper trading.
        """
        workstation = getattr(self, "workstation", None)
        if workstation is None:
            return
        try:
            history = self._history.get(candle.symbol, [])
            price_change_pct = None
            if len(history) >= 2 and history[-2].close > 0:
                price_change_pct = history[-1].close / history[-2].close - 1.0
            volume_ratio = None
            volumes = [float(getattr(c, "volume", 0.0) or 0.0) for c in history]
            if len(volumes) >= 21:
                recent = sum(volumes[-5:]) / 5.0
                prior = sum(volumes[-21:-5]) / 16.0
                if prior > 0:
                    volume_ratio = recent / prior
            workstation.opportunities.observe(
                market_id=str(getattr(workstation.settings, "market", "india")),
                identity=str(candle.symbol),
                price_change_pct=price_change_pct,
                volume_ratio=volume_ratio)
        except Exception:  # pragma: no cover - telemetry must not break paper
            logger.exception("workstation observation failed (telemetry only)")

    async def _prepare_history(self) -> None:
        """Prime indicators from completed history before the live loop starts.

        Only providers that expose ``warmup_bars`` participate (currently the
        Alpaca adapter). Every seeded bar is already complete at call time and
        is filtered against the broker's own cursor, so this cannot look ahead.
        A failure degrades to an empty warm-up; it never fabricates a bar.
        """
        warmup = getattr(self.provider, "warmup_bars", None)
        if warmup is None or not self.symbols:
            return
        need = int(getattr(self.strategy, "history_limit", 0) or 0)
        if need <= 0:
            return
        if all(len(self._history.get(symbol, [])) >= need for symbol in self.symbols):
            return
        try:
            bars = await warmup(min(need, 1000))
        except Exception as exc:  # pragma: no cover — provider-specific failure mode
            logger.warning("paper_warmup_failed", extra={"error": type(exc).__name__})
            return
        for symbol in self.symbols:
            existing = self._history.setdefault(symbol, [])
            if existing:
                continue
            cursor = self.broker.last_bars.get(symbol)
            floor = parse_timestamp(cursor) if cursor else None
            seeded = [
                bar for bar in bars
                if bar.symbol == symbol and (floor is None or bar.timestamp > floor)
            ][-need:]
            if seeded:
                self._history[symbol] = seeded
                logger.info("paper_warmup", extra={"symbol": symbol,
                                                   "bars": len(seeded)})

    async def _run(self):
        self.state = BotState.RUNNING
        phase = "provider_stream"
        try:
            await self._prepare_history()
            async for candle in self.provider.stream(self.symbols, self.broker.timeframe):
                # Replay resumes after the committed cursor. Live out-of-order data fails closed.
                last = self.broker.last_bars.get(candle.symbol)
                if (
                    isinstance(self.provider, ReplayStream)
                    and last
                    and candle.timestamp < parse_timestamp(last)
                ):
                    continue
                phase = "bar_processing"
                self.process_bar(candle)
                phase = "provider_stream"
        except asyncio.CancelledError:
            pass  # documented: loop exits on task cancellation
        except Exception as exc:
            self._record_failure(exc, phase)
        finally:
            try:
                self.broker.cancel_all()
            except Exception as exc:
                if self.state != BotState.ERROR:
                    self._record_failure(exc, "broker_cleanup")
            try:
                await self.provider.close()
            except Exception as exc:
                if self.state != BotState.ERROR:
                    self._record_failure(exc, "provider_cleanup")
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
        self.state = BotState.RUNNING
        self.error = self.error_phase = self.error_reason = None
        return self.status()

    def request_stop(self) -> None:
        """Ask the run loop to finish WITHOUT doing any I/O.

        This is what a signal handler should call. Stopping properly means
        cancelling open orders and persisting the final broker state, which
        requires the repository writer — and the writer is released by
        ``broker.close()`` at the very end of shutdown. A handler that ran the
        full stop path could therefore race the ordered shutdown and try to
        write after ownership was released.

        Cancelling the stream task is enough: the run loop exits, the caller
        falls through to the single awaited ``stop()``, and the ordered
        shutdown happens exactly once, while the writer is still held.
        """
        if self.task and not self.task.done():
            self.task.cancel()

    async def stop(self):
        """Stop the loop, cancel working orders and persist. Idempotent.

        Being idempotent matters: Ctrl+C reaches both the signal handler and
        the shutdown path, so a naive implementation ran the whole sequence
        twice and the second pass failed on a repository it no longer owned,
        printing a RuntimeError traceback at the end of an otherwise clean
        session.
        """
        if self._stopped:
            return self.status()
        self._stopped = True

        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass  # the loop was cancelled on purpose
        self.broker.cancel_all()
        self.state = BotState.STOPPED
        return self.status()
