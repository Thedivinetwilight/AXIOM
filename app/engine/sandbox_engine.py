"""24/7 trading engine for the Upstox execution modes (V1 sandbox, V2 gated LIVE).

The mode is the BROKER's own identity: an UpstoxSandboxBroker routes to a
sandbox host and records sandbox audit rows; an UpstoxLiveBroker routes to the
documented live order host and records LIVE audit rows. Real money is reachable
ONLY when Settings accepted the four-switch opt-in (MODE=LIVE,
LIVE_TRADING=true, BROKER=UPSTOX_LIVE, LIVE_CONFIRM) plus explicit non-zero
hard risk limits — see LIVE_TRADING_ENABLEMENT.md. The engine itself can never
enable live trading, alter risk limits, or pick a host.

Pipeline per completed candle (market OPEN only, provider gate respected):

    MarketEvent → completed candle → strategy → regime/journal
    → HARD risk gate (outside learning) → Upstox SANDBOX order
    → documented reconciliation → local ledger → SQLite

Outside market hours the engine reconciles once more, runs the rolling
learning/evaluation batch (which can only promote VALIDATED model versions)
and idles. Restart safety:
- submissions are deduplicated by deterministic intent ids (uuid5 of
  strategy+symbol+candle timestamp+side) persisted before/after submission;
- on restart, open sandbox orders are restored from the audit tables and
  reconciled — never resubmitted;
- an EMERGENCY STOP flag is persisted in engine_state and survives restarts
  (explicit resume required).

Hard risk limits come only from Settings; the learning layer cannot modify
them, and nothing in this engine can enable live trading.
"""

import logging
import math
import time
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.risk.limits import HardRiskLimits, OrderIntent
from app.risk.net_edge import NetEdgeGate
from app.research.quality_scorer import TradeQualityScorer
from app.data.liquidity import LiquidityIntelligence
from app.learning.volatility_sizing import VolatilityAwareSizer, SizingContext
from app.data.cross_market import CrossMarketCache
from app.research.entry_timing import EntryTimingEngine
from app.research.alpha_decay import AlphaDecayTracker
from app.engine.hot_path import HotPathMonitor, ColdPathQueue
from app.utils.numerical import is_valid_price, is_valid_quantity, sanitize_candle

logger = logging.getLogger("bot.engine")

FINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}


def intent_id_for(strategy: str, symbol: str, candle, side: str) -> str:
    """Deterministic per-signal id: the same candle can never submit twice."""
    key = f"{strategy}|{symbol}|{candle.timestamp.isoformat()}|{side}"
    return uuid5(NAMESPACE_URL, key).hex


class SandboxEngine:
    """Orchestrates data → strategies → risk → sandbox orders → reconciliation."""

    def __init__(self, *, provider, strategy, broker, repository,
                 risk_limits: HardRiskLimits, journal, timeframe: str = "1m",
                 evaluate_every: int = 30, sizing_fraction: float = 0.25,
                 regime_engine=None, shadow_engine=None,
                 event_engine=None, pause_gate=None, monitor=None,
                 maintenance_scheduler=None, strategy_source=None,
                 allocator=None, instrument_keys=None,
                 live_ops=None,
                 market_timezone: str = "Asia/Kolkata",
                 stop_loss: float | None = 0.0,
                 take_profit: float | None = 0.0,
                 cost_rate: float = 0.001,
                 net_edge_gate=None,
                 quality_scorer=None,
                 liquidity_intel=None,
                 volatility_sizer=None,
                 cross_market_cache=None,
                 entry_timing=None,
                 alpha_decay=None,
                 hot_path_monitor=None,
                 cold_queue=None,
                 settings=None):
        if timeframe not in {"1m", "5m", "15m", "30m"}:
            raise ValueError("Engine supports intraday timeframes")
        self.provider = provider
        self.strategy = strategy
        self.broker = broker
        self.repository = repository
        self.risk_limits = risk_limits
        self.journal = journal
        self.timeframe = timeframe
        self.evaluate_every = max(1, int(evaluate_every))
        self.sizing_fraction = float(sizing_fraction)
        self.state = "STOPPED"
        self.processed = 0
        self.submitted = 0
        self.rejected_by_risk = 0
        # V2.1 L-ops: single-engine lock. Enforced for LIVE engines only;
        # paper/sandbox runs record it but are never blocked by it.
        self.live_lock = None
        if live_ops is not None and hasattr(live_ops, "make_lock"):
            try:
                self.live_lock = live_ops.make_lock()
            except Exception:  # pragma: no cover - a lock must not block start
                logger.exception("could not construct the engine lock")
        # V2.1 L-ops: optional live safety rails (guards/watchdog/breaches).
        # None = the paper/sandbox behaviour is byte-for-byte unchanged.
        self.live_ops = live_ops
        self.signals: dict = {}
        self.regime: str | None = None
        self._history: list = []
        self._peak_equity = 0.0
        self._trades_today = 0
        self._last_realized = 0.0
        self.evaluation: dict | None = None
        self.regime_engine = regime_engine
        self.shadow_engine = shadow_engine
        self.event_engine = event_engine
        self.pause_gate = pause_gate
        self.monitor = monitor
        self.maintenance_scheduler = maintenance_scheduler
        # --- 3.0.1: adaptive selection, allocation, daily + crash safety ---
        self.strategy_source = strategy_source
        self.allocator = allocator
        self.stop_loss = float(stop_loss or 0.0)
        self.take_profit = float(take_profit or 0.0)
        self.cost_rate = float(cost_rate)
        self.last_router_decision: dict | None = None
        self.last_events: list = []
        self._recovery_report: dict = {}
        from app.engine.state import SubmissionJournal, DailyRiskAccount, \
            recover_ledger_state

        self.intent_journal = SubmissionJournal(repository) \
            if repository is not None else None
        if live_ops is not None:
            live_ops.attach_journal(self.intent_journal)
        self.daily = DailyRiskAccount(repository, market_timezone) \
            if repository is not None else None
        # authoritative symbol → instrument key resolution.
        # 3.0.2: the broker default key is NEVER used to fabricate a mapping;
        # identity comes from the explicit map / the provider's own mapping.
        # An unresolved symbol is rejected at the execution boundary.
        keys = dict(instrument_keys or {})
        if not keys:
            key_to_symbol = getattr(provider, "_symbol_for", {}) or {}
            keys = {symbol: key for key, symbol in key_to_symbol.items()}
        if keys and hasattr(broker, "instrument_map"):
            broker.instrument_map.update(keys)
        self.instrument_keys = keys
        # 3.0.2: with a strategy_source, ENTRIES require an explicit eligible
        # router selection; the constructor strategy is diagnostics-only then.
        self._router_authorized = strategy_source is None
        self._seen_closed = False  # instance state (never shared class-wide)
        # 3.0.3: the ACTIVE strategy's candle cadence is registered here and
        # re-registered on every router strategy swap — never name-hardcoded.
        self._sync_cadence()
        # 3.0.5: performance / trade-quality gates (additive, never bypass hard risk)
        try:
            from app.config.settings import Settings as _Settings
            _s = settings or (_Settings.from_env() if repository is not None else None)
        except Exception:
            _s = settings
        self.net_edge_gate = net_edge_gate or (NetEdgeGate.from_settings(_s) if _s else NetEdgeGate())
        self.quality_scorer = quality_scorer or (TradeQualityScorer.from_settings(_s) if _s else TradeQualityScorer())
        self.liquidity_intel = liquidity_intel or (LiquidityIntelligence.from_settings(_s) if _s else LiquidityIntelligence())
        self.volatility_sizer = volatility_sizer or (VolatilityAwareSizer.from_settings(_s) if _s else VolatilityAwareSizer())
        self.cross_market = cross_market_cache or (CrossMarketCache.from_settings(_s) if _s else CrossMarketCache())
        self.entry_timing = entry_timing or (EntryTimingEngine.from_settings(_s) if _s else EntryTimingEngine())
        self.alpha_decay = alpha_decay or (AlphaDecayTracker.from_settings(_s) if _s else AlphaDecayTracker())
        self.hot_path = hot_path_monitor or (HotPathMonitor.from_settings(_s) if _s else HotPathMonitor())
        self.cold_queue = cold_queue or ColdPathQueue()
        try:
            from app.research.mae_mfe import MAEMFEAnalyzer
            from app.learning.correlation import CorrelationMonitor
            from app.learning.degradation import DegradationMonitor
            from app.research.scanner_funnel import ScannerFunnel
            self.mae_mfe = MAEMFEAnalyzer()
            self.correlation_monitor = CorrelationMonitor.from_settings(_s) if _s else CorrelationMonitor()
            self.degradation_monitor = DegradationMonitor.from_settings(_s) if _s else DegradationMonitor()
            self.scanner_funnel = ScannerFunnel.from_settings(_s) if _s else ScannerFunnel()
        except Exception:
            self.mae_mfe = None
            self.correlation_monitor = None
            self.degradation_monitor = None
            self.scanner_funnel = None
        # durable account recovery BEFORE anything can trade
        if repository is not None:
            self._recovery_report = recover_ledger_state(
                broker.ledger, repository, live=bool(getattr(broker, "LIVE", False)))
            if self._recovery_report.get("orders_replayed"):
                logger.warning("LEDGER_RECOVERED",
                               extra={"orders": self._recovery_report["orders_replayed"]})
        self.restore_from_repository()
        self._surface_unresolved_intents()
        self._snapshot_dates: set[str] = set()

    # -- restart recovery ----------------------------------------------------

    def _sync_cadence(self) -> None:
        """Ensure the ACTIVE strategy's candle cadence is registered (3.0.3).

        The normal cadence (every candle, unless a strategy declares its own
        ``cadence`` attribute) belongs to whatever strategy is currently
        routed — never to a hard-coded strategy name. An explicitly
        registered cadence (operator/strategy choice) is NEVER clobbered; a
        routed strategy with no cadence yet receives the every-candle
        default. Called at construction and re-run on every strategy change
        so execution/evaluation rhythm stays consistent with the active
        strategy.
        """
        if self.event_engine is None:
            return
        declared = getattr(self.strategy, "cadence", None)
        if declared is not None:
            self.event_engine.register_cadence(self.strategy.name,
                                               max(1, int(declared)))
            return
        if self.event_engine.cadence_for(self.strategy.name) is None:
            self.event_engine.register_cadence(self.strategy.name, 1)

    def restore_from_repository(self):
        """Re-register open sandbox orders after a restart; never resubmit.

        3.0.3: restores the COMPLETE child (slice) order-id list plus each
        child's fill/status baseline from the recorded transitions, so
        post-restart reconciliation applies only genuinely NEW deltas — no
        child order is lost and no already-applied fill is applied twice.
        """
        restored = 0
        if self.repository is None or not hasattr(self.broker, "orders"):
            return restored
        open_orders = (self.repository.open_live_orders()
                       if getattr(self.broker, "LIVE", False)
                       else self.repository.open_sandbox_orders())
        for record in open_orders:
            intent_id = record.get("intent_id")
            if not intent_id or intent_id in self.broker.orders:
                continue
            from app.brokers.sandbox import SandboxOrderState

            transitions = record.get("transitions") or []
            # COMPLETE child list: payload list first, then every child id
            # recorded on a transition, then the legacy single-id column.
            child_ids: list[str] = []
            for oid in record.get("upstox_order_ids") or []:
                if oid and str(oid) not in child_ids:
                    child_ids.append(str(oid))
            for transition in transitions:
                oid = transition.get("upstox_order_id")
                if oid and str(oid) not in child_ids:
                    child_ids.append(str(oid))
            column_id = record.get("upstox_order_id")
            if column_id and str(column_id) not in child_ids:
                child_ids.append(str(column_id))
            # per-child baselines: the latest authoritative cumulative fill
            # per child matches exactly what recover_ledger_state replayed,
            # so new deltas after the restart apply exactly once.
            child_filled: dict[str, float] = {}
            child_avg: dict[str, float] = {}
            child_status: dict[str, str] = {}
            for transition in transitions:
                oid = transition.get("upstox_order_id")
                if not oid:
                    continue
                oid = str(oid)
                filled = float(transition.get("filled_quantity") or 0)
                if filled >= child_filled.get(oid, 0.0):
                    child_filled[oid] = filled
                    child_avg[oid] = float(transition.get("average_price") or 0.0)
                status = str(transition.get("status") or "")
                if status in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED",
                              "FILLED", "CANCELLED", "REJECTED"}:
                    child_status[oid] = status
            state = SandboxOrderState(
                our_id=record["id"], symbol=record["symbol"], side=record["side"],
                quantity=record["quantity"], price=record.get("price") or 0.0,
                status="SUBMITTED",
                upstox_order_ids=child_ids,
                child_status=child_status,
            )
            state.child_filled = child_filled
            state.child_avg_price = child_avg
            state.filled_quantity = sum(child_filled.values())
            self.broker.orders[intent_id] = state
            restored += 1
        if restored:
            logger.info(
                "LIVE_ORDERS_RESTORED" if getattr(self.broker, "LIVE", False)
                else "SANDBOX_ORDERS_RESTORED", extra={"count": restored})
        return restored

    def _surface_unresolved_intents(self):
        """Crash recovery: intents whose remote fate is unknown are surfaced
        for reconciliation and are NEVER resubmitted (duplicate safety)."""
        if self.intent_journal is None:
            return
        unresolved = self.intent_journal.unresolved()
        for intent_id, entry in unresolved.items():
            self.repository.record_system_event(
                "RECONCILE_REQUIRED",
                {"intent_id": intent_id, "symbol": entry.get("symbol"),
                 "side": entry.get("side"), "quantity": entry.get("quantity"),
                 "state": entry.get("state"),
                 "note": "remote outcome uncertain; blocked from resubmission"})
        if unresolved:
            logger.warning("UNRESOLVED_INTENTS", extra={"count": len(unresolved)})

    # -- safety --------------------------------------------------------------

    def emergency_stop(self, reason: str = "manual") -> dict:
        """Persisted emergency stop: cancels open sandbox orders and halts."""
        self.repository.set_engine_state(
            "emergency_stop", {"stopped": True, "ts": datetime.now(timezone.utc).isoformat(),
                               "reason": reason}
        )
        self.repository.record_system_event("EMERGENCY_STOP", {"reason": reason})
        cancelled = []
        for intent_id, state in list(self.broker.orders.items()):
            if state.status in FINAL_STATUSES:
                continue
            try:
                self.broker.cancel(intent_id)
                cancelled.append(state.our_id)
            except Exception as exc:  # never hide a cancel failure
                self.repository.record_system_event(
                    "CANCEL_FAILURE", {"order": state.our_id, "error": type(exc).__name__}
                )
        self.state = "EMERGENCY_STOPPED"
        logger.error("EMERGENCY_STOP", extra={"reason": reason})
        return {"stopped": True, "cancelled": cancelled}

    def resume(self) -> dict:
        self.repository.set_engine_state("emergency_stop", {"stopped": False})
        self.repository.record_system_event("ENGINE_RESUME", {})
        self.state = "STOPPED"
        return {"stopped": False}

    def _check_emergency(self) -> bool:
        flag = self.repository.get_engine_state("emergency_stop", {}) or {}
        return bool(flag.get("stopped"))

    # -- main loop -----------------------------------------------------------

    def run(self, *, max_candles: int | None = None, force_lock: bool = False):
        """Consume the provider stream until it ends / max_candles / stop flag.

        Refuses to start when another LIVE engine is heartbeating against the
        same database: two live engines on one account double every entry.
        """
        if self.live_lock is not None:
            decision = self.live_lock.acquire(force=force_lock)
            if not decision.granted:
                self.state = "REFUSED_DUPLICATE_ENGINE"
                self.repository.record_system_event(
                    "ENGINE_REFUSED_DUPLICATE",
                    {"reason": decision.reason,
                     "existing": (decision.existing.to_dict()
                                  if decision.existing else None)})
                logger.error("ENGINE_REFUSED_DUPLICATE",
                             extra={"reason": decision.reason})
                return {"ok": False, "reason": decision.reason,
                        "lock": decision.to_dict(), **self.status()}
        self.state = "RUNNING"
        try:
            import asyncio

            async def consume():
                async for candle in self.provider.stream(self.provider.symbols, self.timeframe):
                    if self._check_emergency():
                        self.state = "EMERGENCY_STOPPED"
                        break
                    if self.maintenance_scheduler is not None:
                        try:
                            self.maintenance_scheduler.run_due()
                        except Exception:  # maintenance must never kill the loop
                            logger.exception("maintenance job failed")
                    self._ops_tick()
                    self.process_candle(candle)
                    if max_candles is not None and self.processed >= max_candles:
                        break

            asyncio.run(consume())
            self.reconcile()
            self._maybe_evaluate(force=self._seen_closed)
            if self.monitor is not None:
                self.monitor.snapshot()
        finally:
            if self.state == "RUNNING":
                self.state = "STOPPED"
            if self.live_lock is not None:
                self.live_lock.release()
        return self.status()

    def _ops_tick(self, *, now=None) -> None:
        """Observe operational health and escalate breaches (V2.1 L-ops).

        Runs every loop iteration — including when the market is closed —
        because the failures worth catching (a stuck order, an auth fail,
        reconciliation going quiet) happen precisely while nothing is being
        traded. Never raises: an operations check must not break the loop.
        """
        if self.live_ops is None:
            return
        now = now or datetime.now(timezone.utc)
        live_lock = getattr(self, "live_lock", None)
        if live_lock is not None:
            # proving this engine is alive is what makes a stale lock harmless
            live_lock.heartbeat(now=now)
        try:
            findings = self.live_ops.observe(now=now)
            self._auto_cancel_escalated_stuck_orders(findings)
            self._retry_failed_alerts_once(now=now)
        except Exception:  # pragma: no cover - defensive
            logger.exception("live_ops observe failed")
        try:
            equity = self.broker.ledger.equity()
            realized_today = (self.daily.realized_pnl if self.daily is not None
                              else self._last_realized)
            breach = self.live_ops.breach(
                realized_today=realized_today, equity=equity,
                peak_equity=self._peak_equity or None)
        except Exception:  # pragma: no cover - defensive
            logger.exception("live_ops breach check failed")
            breach = None
        if breach and breach.get("stop") and not self._check_emergency():
            self.emergency_stop(
                reason=f"AUTO_STOP {breach['kind']}: {breach['message']}")

    def _retry_failed_alerts_once(self, *, now=None) -> dict:
        """Give a failed page another chance on the ops cadence.

        Paging is the one rail that depends on a third party being reachable;
        a single transient 502 must not be the difference between an operator
        knowing and not knowing. Bounded by the router's attempt cap.
        """
        result = self.live_ops.retry_failed_alerts(now=now)
        if result.get("delivered") or result.get("dropped"):
            logger.warning("ALERT_RETRY_TICK", extra={"result": result})
        return result

    def _auto_cancel_escalated_stuck_orders(self, findings) -> list[str]:
        """Cancel orders the watchdog has escalated, when explicitly enabled.

        The watchdog only *reports*; acting is the engine's job (it owns the
        broker). This runs only when ``LIVE_AUTO_CANCEL_STUCK`` is on — the
        default is to alert a human, never to cancel silently. Failures are
        recorded, never swallowed, and a cancel that cannot be confirmed leaves
        the order working (the broker decides, not this method).
        """
        if not getattr(self.live_ops, "auto_cancel_stuck", False) or not findings:
            return []
        targets = self.live_ops.escalated_stuck_order_ids(findings)
        if not targets:
            return []
        cancelled: list[str] = []
        for intent_id, state in list(self.broker.orders.items()):
            if state.our_id not in targets or state.status in FINAL_STATUSES:
                continue
            try:
                self.broker.cancel(intent_id)
                cancelled.append(state.our_id)
                self.repository.record_system_event(
                    "STUCK_ORDER_AUTO_CANCEL",
                    {"order": state.our_id, "intent_id": intent_id,
                     "reason": "escalated by the L-ops watchdog"})
                logger.warning("STUCK_ORDER_AUTO_CANCEL", extra={"order": state.our_id})
            except Exception as exc:  # a failed cancel must never be silent
                self.repository.record_system_event(
                    "STUCK_ORDER_AUTO_CANCEL_FAILED",
                    {"order": state.our_id, "error": type(exc).__name__})
                logger.exception("STUCK_ORDER_AUTO_CANCEL_FAILED")
        return cancelled

    def process_candle(self, candle) -> None:
        metrics = self.provider.metrics()
        if not metrics.get("market_open", False):
            self._seen_closed = True
            self.repository.record_system_event(
                "MARKET_CLOSED_SKIP", {"timestamp": candle.timestamp.isoformat()}
            )
            self.reconcile()
            return
        if metrics.get("stale_feed"):
            logger.warning("STALE_FEED_SIGNALS_PAUSED")
            self.reconcile()
            return
        from app.data.validator import validate

        validate([candle], self.timeframe).require_valid()
        self._history.append(candle)
        limit = max(getattr(self.strategy, "history_limit", 30), 5)
        self._history[:] = self._history[-limit:]
        self.processed += 1
        self.ledger_mark(candle.close)
        if self.regime_engine is not None:
            self.regime_engine.evaluate(self._history)
        if self.shadow_engine is not None:
            self.shadow_engine.on_candle(candle)
        self._daily_snapshot(candle)
        if self.daily is not None:
            self.daily.rollover(now=datetime.now(timezone.utc))
        # --- event-driven dispatch (3.0.1): events DECIDE what is evaluated ---
        self.last_events = self.event_engine.on_candle(
            candle, self._history, self.regime_engine) \
            if self.event_engine is not None else []
        triggers = [decision.trigger for decision in self.last_events]
        # protective exits run FIRST and independently of strategy signals
        self._check_protective_exits(candle)
        if self.processed % self.evaluate_every == 0:
            self._maybe_evaluate(force=True)  # scheduled rolling batch
        self.regime = classify_regime_safe(self._history)
        self._maybe_swap_strategy()
        if not self._strategy_evaluation_due(triggers):
            self.signals[candle.symbol] = {
                "signal": "HOLD", "timestamp": candle.timestamp.isoformat(),
                "reason": "no event due (debounced)"}
            return
        signal = self.strategy.generate_signal(tuple(self._history))
        self.signals[candle.symbol] = {"signal": signal.value, "timestamp": candle.timestamp.isoformat()}
        self._act(signal, candle, triggers=triggers)

    def _strategy_evaluation_due(self, triggers: list[str]) -> bool:
        """Cadence + event triggers decide whether the strategy is evaluated.

        With the default cadence (every candle) this preserves the historic
        behavior; a coarser registered cadence debounces trivial candles while
        significant events still force an out-of-band evaluation.
        """
        if self.event_engine is None:
            return True  # legacy per-candle evaluation
        cadence_due = any(decision.trigger == "cadence"
                          and decision.strategy in {"*", self.strategy.name}
                          for decision in self.last_events)
        event_due = any(decision.trigger in {"significant_move", "volume_spike",
                                             "volatility_change", "regime_change",
                                             "order_event"}
                        for decision in self.last_events)
        return bool(cadence_due or event_due)

    def _maybe_swap_strategy(self):
        """Router-driven adaptive selection (3.0.1). The selected candidate
        actually controls the next trades; every change is versioned."""
        if self.strategy_source is None:
            return
        decision, strategy_instance = self.strategy_source.select(self.regime)
        self.last_router_decision = decision
        # executable ⇔ explicit eligible selection THIS cycle; entries stop
        # the moment no qualified candidate exists (degraded/retired/absent)
        self._router_authorized = bool(
            decision and decision.get("selected") and decision.get("executable")
            and strategy_instance is not None)
        if decision is None or not decision.get("selected") \
                or strategy_instance is None:
            return
        # persist every ACCEPTED router decision as a versioned learning event
        self.repository.record_learning_event("ROUTER_DECISION", {
            "ts": decision.get("ts"), "regime": self.regime,
            "strategy": decision.get("strategy"),
            "parameters": decision.get("parameters"),
            "candidate_id": decision.get("candidate_id"),
            "confidence": decision.get("confidence"),
            "reason": decision.get("reason")})
        current_version = (f"{self.strategy.name}-"
                           f"{sorted(self.strategy.parameters.items())}").replace("'", "")
        candidate_version = (f"{strategy_instance.name}-"
                             f"{sorted(strategy_instance.parameters.items())}").replace("'", "")
        if candidate_version != current_version:
            previous = {"name": self.strategy.name,
                        "parameters": dict(self.strategy.parameters)}
            self.strategy = strategy_instance
            limit = max(getattr(strategy_instance, "history_limit", 30), 5)
            self._history[:] = self._history[-limit:]
            self._sync_cadence()  # 3.0.3: cadence follows the ACTIVE strategy
            self.repository.record_system_event("STRATEGY_SWAPPED", {
                "from": previous, "to": {"name": strategy_instance.name,
                                         "parameters": dict(strategy_instance.parameters)},
                "regime": self.regime,
                "reason": decision.get("reason"),
                "note": "router-selected, positive-OOS candidate; versioned change"})

    def _check_protective_exits(self, candle) -> bool:
        """Stop-loss / take-profit exits, independent of strategy signals.

        Uses the RECOVERED position state (average entry), so exits survive
        restarts. Deterministic per-candle intent ids prevent duplicates.
        """
        ledger = self.broker.ledger
        if ledger.position_quantity <= 0:
            return False
        avg = float(ledger.position_avg_price or 0)
        if avg <= 0 or candle.close <= 0:
            return False
        reason = None
        if self.stop_loss > 0 and candle.close <= avg * (1 - self.stop_loss):
            reason = "PROTECTIVE_STOP_LOSS"
        elif self.take_profit > 0 and candle.close >= avg * (1 + self.take_profit):
            reason = "PROTECTIVE_TAKE_PROFIT"
        if reason is None:
            return False
        # duplicate-exit protection: while ANY sell-side order is still
        # working (submitted but not yet reconciled), never fire another exit
        if any(getattr(order, "side", "") == "SELL"
               for order in self.broker.open_orders()):
            logger.info("PROTECTIVE_EXIT_ALREADY_WORKING")
            return False
        self._submit("SELL", ledger.position_quantity, candle.close, candle,
                     reason=reason)
        return True

    def _act(self, signal, candle, triggers=None) -> None:
        from app.strategies.base import Signal

        triggers = triggers or []
        if signal == Signal.HOLD:
            logger.debug(
                "SIGNAL_NOT_AN_ORDER",
                extra={"reason": "HOLD", "symbol": candle.symbol,
                       "price": candle.close, "processed": self.processed},
            )
            return
        params = dict(self.strategy.parameters)
        self.journal.record_signal(
            strategy=self.strategy.name, symbol=candle.symbol, timeframe=self.timeframe,
            signal=signal.value, price=candle.close, candles=self._history,
            meta={"processed": self.processed, "triggers": triggers,
                  "regime": self.regime,
                  "params_version": self.journal.params_version},
        )
        ledger = self.broker.ledger
        if signal in {Signal.SELL, Signal.EXIT}:
            if ledger.position_quantity > 0:
                self._submit("SELL", ledger.position_quantity, candle.close, candle,
                             reason="STRATEGY_EXIT")
            else:
                logger.info(
                    "SIGNAL_NOT_AN_ORDER",
                    extra={"reason": "EXIT_WITH_NO_POSITION",
                           "signal": signal.value, "symbol": candle.symbol,
                           "price": candle.close, "processed": self.processed},
                )
            return
        # --- BUY: size FIRST, then gate the EXACT final quantity (3.0.1) -----
        # 3.0.2: with a strategy source, entries require an ELIGIBLE router
        # selection (full lifecycle, positive OOS). No qualified candidate
        # ⇒ NO entry — the constructor strategy never self-authorizes.
        if self.strategy_source is not None and not self._router_authorized:
            self.rejected_by_risk += 1
            self.repository.record_system_event(
                "RISK_REJECT", {"reason": "NO_QUALIFIED_CANDIDATE",
                                "ts": candle.timestamp.isoformat(),
                                "symbol": candle.symbol,
                                "strategy": self.strategy.name})
            logger.warning("NO_QUALIFIED_CANDIDATE")
            return
        if ledger.position_quantity > 0:
            logger.info("POSITION_ALREADY_OPEN")  # no pyramiding
            return
        # never stack entries while an entry order is still working: treat as
        # a visible risk rejection (duplicate prevention + observability)
        if any(getattr(order, "side", "") == "BUY"
               for order in self.broker.open_orders()):
            self.rejected_by_risk += 1
            self.repository.record_system_event(
                "RISK_REJECT", {"reason": "ENTRY_ORDER_STILL_WORKING",
                                "ts": candle.timestamp.isoformat(),
                                "symbol": candle.symbol})
            logger.info("ENTRY_ORDER_STILL_WORKING")
            return
        quantity, allocated, est_fees, sizing_note = self._size_entry(candle)
        if quantity < 1:
            self.repository.record_system_event("SIZING_SKIPPED", {
                "reason": sizing_note, "symbol": candle.symbol,
                "ts": candle.timestamp.isoformat()})
            return
        # --- 3.0.5: volatility-aware sizing (before hard risk, never exceeds hard cap) ---
        try:
            # Estimate volatility from history (simple close-to-close std)
            vol = None
            atr = None
            if len(self._history) >= 5:
                closes = [float(c.close) for c in self._history[-20:]]
                rets = [(closes[i]/closes[i-1]-1) for i in range(1, len(closes)) if closes[i-1] > 0]
                if len(rets) >= 3:
                    import statistics
                    try:
                        vol = statistics.pstdev(rets) if len(rets) > 1 else 0.01
                    except Exception:
                        vol = 0.015
                # ATR proxy: avg true range / price
                try:
                    trs = [max(float(c.high)-float(c.low), abs(float(c.high)-closes[i-1]), abs(float(c.low)-closes[i-1])) for i, c in enumerate(self._history[-10:]) if i>0]
                    atr = (sum(trs)/len(trs)) if trs else None
                except Exception:
                    atr = None
            # Liquidity snapshot for sizing
            liq_score = None
            try:
                # Try to get bid/ask from provider metrics or last candle
                bid = getattr(candle, "bid", None)
                ask = getattr(candle, "ask", None)
                # Use current time for liquidity to avoid historical stale rejection
                snap = self.liquidity_intel.assess(candle.symbol, bid=bid, ask=ask, last_price=candle.close, volume=float(getattr(candle, "volume", 0) or 0), volatility=vol, timestamp=time.time())
                liq_score = snap.liquidity_score
            except Exception:
                liq_score = 0.7
            # Exposure
            exp_ratio = None
            try:
                exp = self.broker.ledger.equity()
                gross = sum(abs(float(p.get("quantity",0))*float(p.get("price",0))) for p in getattr(self.broker.ledger, "positions", []) or [])
                exp_ratio = gross/exp if exp>0 else 0
            except Exception:
                exp_ratio = 0
            # Hard cap quantity for sizing: do not pre-cap here; let HardRiskLimits be the
            # authoritative gate (test expects oversized 1000 to be rejected, not silently capped to 50)
            hard_cap_q = None
            sizing_ctx = SizingContext(base_quantity=float(quantity), price=float(candle.close), volatility=vol, atr=atr, regime=self.regime, confidence=float(self.last_router_decision.get("confidence",0.5)) if self.last_router_decision else 0.5, expected_edge=None, stop_distance=self.stop_loss or 0.05, liquidity_score=liq_score, exposure_ratio=exp_ratio, hard_cap_quantity=hard_cap_q)
            adj_qty, sizing_detail = self.volatility_sizer.adjust(sizing_ctx)
            if adj_qty < quantity:
                self.repository.record_system_event("VOLATILITY_SIZING", {"original": quantity, "adjusted": adj_qty, "detail": sizing_detail, "symbol": candle.symbol})
            quantity = int(adj_qty)
            if quantity < 1:
                self.repository.record_system_event("VOLATILITY_SIZING_SKIP", {"reason": sizing_detail, "symbol": candle.symbol})
                return
            # Recompute fees for adjusted quantity
            est_fees = round(quantity * candle.close * self.cost_rate, 4)
        except Exception as exc:
            # Sizing must never block on error; fall back to original quantity
            import logging as _lg
            _lg.getLogger("bot.engine").debug("volatility_sizing_failed", extra={"error": type(exc).__name__})

        # --- 3.0.5: cross-market context (lightweight, no second data system) ---
        cross_ctx = None
        try:
            cross_ctx = self.cross_market.context_for(candidate_symbol=candle.symbol, sector=None)
        except Exception:
            cross_ctx = None

        # --- 3.0.5: liquidity / spread intelligence (candidate-only, cached) ---
        try:
            # Use candle bid/ask if available, else estimate
            bid = getattr(candle, "bid", None)
            ask = getattr(candle, "ask", None)
            # If no bid/ask, synthesize from close and spread config
            if bid is None or ask is None:
                # Use configured spread as proxy
                mid = float(candle.close)
                half = mid * 0.0001  # 1 bps half-spread default
                bid, ask = mid - half, mid + half
            liq_snap = self.liquidity_intel.assess(candle.symbol, bid=bid, ask=ask, last_price=float(candle.close), volume=float(getattr(candle, "volume", 0) or 0), volatility=vol if 'vol' in locals() else None, timestamp=time.time())
            # Store for later use in quality/net edge
            self._last_liquidity = liq_snap
            should_reject, should_reduce, liq_reason = self.liquidity_intel.should_reduce_or_reject(liq_snap)
            if should_reject:
                self.repository.record_system_event("LIQUIDITY_REJECT", {"symbol": candle.symbol, "spread_bps": liq_snap.spread_bps, "liquidity_score": liq_snap.liquidity_score, "reason": liq_reason})
                return
        except Exception as exc:
            liq_snap = None
            import logging as _lg2
            _lg2.getLogger("bot.engine").debug("liquidity_check_failed", extra={"error": type(exc).__name__})

        # --- 3.0.5: net edge gate (immediately before hard risk, fail closed) ---
        try:
            # Estimate gross expected from recent OOS or fixed
            gross_expected = None
            win_prob = None
            win_mag = None
            loss_mag = None
            if self.last_router_decision:
                try:
                    conf = float(self.last_router_decision.get("confidence", 0.5))
                    win_prob = 0.5 + (conf - 0.5) * 0.3
                    base_win = 0.015
                    base_loss = 0.01
                    if 'vol' in locals() and vol:
                        base_win *= (1 + vol*10)
                        base_loss *= (1 + vol*10)
                    win_mag = base_win * float(candle.close) * quantity
                    loss_mag = base_loss * float(candle.close) * quantity
                except Exception:
                    win_prob = None
            if gross_expected is None and win_prob is not None and win_mag is not None and loss_mag is not None:
                gross_expected = win_prob * win_mag - (1-win_prob) * loss_mag
            elif gross_expected is None:
                # Fallback: use 2% * notional as gross (covers 1% fees + spread + volatility)
                gross_expected = 0.02 * float(candle.close) * quantity
            # Spread from liquidity snapshot; fallback to tight 2 bps (synthetic spread is 2 bps)
            spread_bps = liq_snap.spread_bps if 'liq_snap' in locals() and liq_snap and liq_snap.spread_bps is not None else 5.0
            # Costs: cost_rate is total round-trip, split into legs for realistic one-side cost
            comm_rate = float(self.cost_rate) * 0.5
            slip_rate = float(self.cost_rate) * 0.5
            net_res = self.net_edge_gate.evaluate(gross_expected=gross_expected, price=float(candle.close), quantity=float(quantity), commission_rate=comm_rate, slippage_rate=slip_rate, spread_bps=spread_bps, volatility=vol if 'vol' in locals() else None, holding_period=5)
            if not net_res.passes:
                self.repository.record_system_event("NET_EDGE_REJECT", {"symbol": candle.symbol, "gross": net_res.gross_expected, "net": net_res.net_expected, "threshold": net_res.threshold, "reason": net_res.reason})
                return
        except Exception as exc:
            import logging as _lg3
            _lg3.getLogger("bot.engine").debug("net_edge_failed", extra={"error": type(exc).__name__})

        # --- 3.0.5: trade quality scorer (ranking/context, not overriding hard risk) ---
        try:
            # Gather supporting features
            strat_conf = float(self.last_router_decision.get("confidence", 0.5)) if self.last_router_decision else 0.5
            recent_oos = None
            try:
                # Try to get recent OOS from allocator or repository
                if self.allocator and hasattr(self.allocator, "repository"):
                    # placeholder
                    recent_oos = 0.001
            except Exception:
                recent_oos = None
            # Use cross_ctx for market alignment
            trend_align = None
            if cross_ctx and cross_ctx.market_direction:
                # crude alignment: if market up and signal BUY, align positive
                trend_align = 0.5 if cross_ctx.market_direction == "up" else -0.3 if cross_ctx.market_direction == "down" else 0
            # Event context
            event_ctx = None
            if self.last_events:
                # Take first event's arena_response if any
                try:
                    event_ctx = {"arena_response": "MONITOR"}
                except Exception:
                    event_ctx = None
            # Portfolio correlation
            corr = None
            try:
                if self.correlation_monitor:
                    # Simple: if many positions, correlation high
                    corr = 0.6 if len(getattr(self.broker.ledger, "positions", []) or []) > 1 else 0.2
            except Exception:
                corr = None
            # Alpha decay factor
            decay_factor = None
            try:
                if self.alpha_decay:
                    decay_factor = self.alpha_decay.decay_factor(self.strategy.name, 0)
            except Exception:
                decay_factor = 1.0
            # Stop distance
            stop_dist = self.stop_loss or 0.05
            # Signal age
            sig_age = 0
            # Volatility regime
            vol_regime = cross_ctx.volatility_context if cross_ctx else None
            # Liquidity score
            liq_sc = liq_snap.liquidity_score if 'liq_snap' in locals() and liq_snap else 0.6
            # Expected net edge
            exp_edge = net_res.net_expected if 'net_res' in locals() and net_res else 0.001
            # Reward/risk
            rr = (self.take_profit / self.stop_loss) if self.stop_loss and self.take_profit else 2.0
            q_res = self.quality_scorer.score(strategy_confidence=strat_conf, expected_net_edge=exp_edge, regime_compatibility=0.6, volatility=vol if 'vol' in locals() else None, volatility_regime=vol_regime, liquidity_score=liq_sc, spread_bps=spread_bps if 'spread_bps' in locals() else 10, trend_alignment=trend_align, market_context={"market_direction": cross_ctx.market_direction if cross_ctx else None}, event_context=event_ctx, recent_oos_expectancy=recent_oos, reward_risk=rr, stop_distance=stop_dist, signal_age=sig_age, alpha_decay_factor=decay_factor, portfolio_correlation=corr)
            if not self.quality_scorer.passes(q_res):
                self.repository.record_system_event("QUALITY_REJECT", {"symbol": candle.symbol, "quality_score": q_res.quality_score, "confidence": q_res.confidence, "reasons": q_res.reasons, "penalties": q_res.penalties})
                return
            # Store for attribution
            self._last_quality = q_res
            self._last_net_edge = net_res
        except Exception as exc:
            import logging as _lg4
            _lg4.getLogger("bot.engine").debug("quality_scorer_failed", extra={"error": type(exc).__name__})

        # --- 3.0.5: optimal entry timing (TRADE_NOW / WAIT / NO_TRADE) ---
        try:
            timing_decision = self.entry_timing.evaluate(strategy=self.strategy.name, current_volatility=vol if 'vol' in locals() else None, market_context={"market_direction": cross_ctx.market_direction if cross_ctx else None})
            if timing_decision.action == "NO_TRADE":
                self.repository.record_system_event("ENTRY_TIMING_NO_TRADE", {"symbol": candle.symbol, "reason": timing_decision.reasons})
                return
            elif timing_decision.action == "WAIT":
                self.repository.record_system_event("ENTRY_TIMING_WAIT", {"symbol": candle.symbol, "preferred": timing_decision.preferred_entry, "reason": timing_decision.reasons})
                return
        except Exception as exc:
            import logging as _lg5
            _lg5.getLogger("bot.engine").debug("entry_timing_failed", extra={"error": type(exc).__name__})

        # --- 3.0.5: alpha decay check ---
        try:
            ok, reason = self.alpha_decay.should_trade(self.strategy.name, 0)
            if not ok:
                self.repository.record_system_event("ALPHA_DECAY_REJECT", {"symbol": candle.symbol, "reason": reason})
                return
        except Exception as exc:
                    import logging as _lg
                    _lg.getLogger(__name__).debug("ignored", extra={"error": type(exc).__name__})

        instrument_key = self.instrument_keys.get(candle.symbol)
        if instrument_key is None:
            # 3.0.2 fail-closed routing: never default to the broker key
            self.rejected_by_risk += 1
            self.repository.record_system_event(
                "RISK_REJECT", {"reason": "INSTRUMENT_IDENTITY_MISSING",
                                "ts": candle.timestamp.isoformat(),
                                "symbol": candle.symbol, "side": "BUY"})
            logger.warning("INSTRUMENT_IDENTITY_MISSING",
                           extra={"symbol": candle.symbol})
            return
        intent = OrderIntent(candle.symbol, "BUY", quantity, candle.close,
                             instrument_key=instrument_key,
                             estimated_fees=est_fees)
        decision = self.risk_limits.check(
            intent, open_positions=1 if ledger.position_quantity > 0 else 0,
            open_position_value=ledger.position_quantity * ledger.mark,
            trades_today=self.daily.trades if self.daily else self._trades_today,
            realized_today=self.daily.realized_pnl if self.daily
            else ledger.realized_pnl - self._last_realized,
            unrealized=ledger.unrealized(),
        )
        self.repository.record_system_event("RISK_DECISION", {
            "allowed": decision.allowed, "reason": decision.reason,
            "symbol": candle.symbol, "quantity": quantity,
            "notional": round(intent.notional, 2),
            "estimated_fees": round(est_fees, 4),
            "ts": candle.timestamp.isoformat(),
        })
        if not decision.allowed:
            self.rejected_by_risk += 1
            self.repository.record_system_event(
                "RISK_REJECT", {"reason": decision.reason, "ts": candle.timestamp.isoformat(),
                                "symbol": candle.symbol, "quantity": quantity}
            )
            logger.warning("RISK_DECISION", extra={"decision": "blocked", "reason": decision.reason})
            return
        # the EXACT quantity that passed the gate is what gets submitted
        self._submit("BUY", quantity, candle.close, candle,
                     reason="STRATEGY_ENTRY", triggers=triggers,
                     allocated_capital=allocated, estimated_fees=est_fees)

    def _size_entry(self, candle) -> tuple[int, float, float, str]:
        """Fees-aware sizing BEFORE the risk gate (3.0.1).

        budget = min(free cash, equity × sizing_fraction, allocator allowance)
        quantity = floor(budget / (price × (1 + cost_rate)))  → the estimated
        ALL-IN cost (notional + fees/slippage) can never exceed the budget,
        so fees cannot create hidden cap violations.
        """
        ledger = self.broker.ledger
        if candle.close <= 0:
            return 0, 0.0, 0.0, "non_positive_price"
        budget = min(ledger.cash, ledger.equity() * self.sizing_fraction)
        allowance = self._allocator_allowance()
        if allowance is not None:
            budget = min(budget, allowance)
        all_in_price = candle.close * (1 + self.cost_rate)
        quantity = int(budget / all_in_price) if all_in_price > 0 else 0
        est_fees = round(quantity * candle.close * self.cost_rate, 4)
        note = (f"budget={budget:.2f} allowance={'plan' if allowance is not None else 'sizing_fraction'}")
        return quantity, budget, est_fees, note

    def _allocator_allowance(self) -> float | None:
        """CapitalAllocator participation: the recorded allocation plan caps
        what this strategy may deploy (portfolio limits bind BEFORE risk gate)."""
        if self.allocator is not None:
            plan = self.repository.get_engine_state("capital_allocation") or {}
            allocations = plan.get("allocations") if isinstance(plan, dict) else None
            if allocations and self.strategy.name in allocations:
                return float(allocations[self.strategy.name])
            return None  # no plan yet: sizing falls back to the engine fraction
        plan = self.repository.get_engine_state("capital_allocation") or {}
        allocations = plan.get("allocations") if isinstance(plan, dict) else None
        if allocations and self.strategy.name in allocations:
            return float(allocations[self.strategy.name])
        return None

    def _submit(self, side: str, quantity: float, price: float, candle,
                reason: str = "STRATEGY_ENTRY", triggers=None,
                allocated_capital: float | None = None,
                estimated_fees: float = 0.0) -> None:
        triggers = triggers or []
        # emergency stop blocks EVERYTHING (entries AND protective exits);
        # the pause gate blocks ENTRIES but never risk-reducing exits.
        if self._check_emergency():
            self.repository.record_system_event(
                "EXECUTION_STOPPED_SKIP", {"side": side, "reason": reason,
                                           "ts": candle.timestamp.isoformat()})
            return
        if (self.pause_gate is not None and self.pause_gate.is_paused("execution")
                and side == "BUY"):
            self.repository.record_system_event(
                "EXECUTION_PAUSED_SKIP", {"side": side, "ts": candle.timestamp.isoformat(),
                                          "reason": reason})
            return
        intent_key = intent_id_for(self.strategy.name, candle.symbol, candle,
                                   side)  # V1 invariant format (dedup-safe)
        if self.intent_journal is not None \
                and self.intent_journal.resubmission_forbidden(intent_key):
            logger.info("DUPLICATE_INTENT_BLOCKED", extra={"intent": intent_key[:8]})
            return
        processed = set(self.repository.get_engine_state("processed_signals", []))
        if intent_key in processed:
            logger.info("DUPLICATE_SIGNAL_IGNORED", extra={"intent": intent_key[:8]})
            return
        instrument_key = self.instrument_keys.get(candle.symbol)
        if instrument_key is None:
            # 3.0.2 fail-closed routing: identity is required for EVERY side
            self.rejected_by_risk += 1
            self.repository.record_system_event(
                "RISK_REJECT", {"reason": "INSTRUMENT_IDENTITY_MISSING",
                                "ts": candle.timestamp.isoformat(),
                                "symbol": candle.symbol, "side": side})
            logger.warning("INSTRUMENT_IDENTITY_MISSING",
                           extra={"symbol": candle.symbol, "side": side})
            return
        # V2.1 L-ops: operational guards gate NEW ENTRIES only. Exits and
        # protective stops are never blocked by an operational rail.
        if self.live_ops is not None:
            decision = self.live_ops.guard_entry(
                side=side, quantity=quantity, price=price)
            if not decision.allowed:
                self.rejected_by_risk += 1
                self.repository.record_system_event("ENTRY_GUARD_BLOCKED", {
                    "reason": decision.reason, "side": side,
                    "quantity": quantity, "price": price,
                    "symbol": candle.symbol, "intent_reason": reason,
                    "ts": candle.timestamp.isoformat()})
                logger.warning("ENTRY_GUARD_BLOCKED",
                               extra={"reason": decision.reason[:120]})
                return
            if decision.adjusted_quantity is not None:
                self.repository.record_system_event("ENTRY_GUARD_REDUCED", {
                    "reason": decision.reason, "side": side,
                    "from_quantity": quantity,
                    "to_quantity": decision.adjusted_quantity,
                    "price": price, "symbol": candle.symbol,
                    "ts": candle.timestamp.isoformat()})
                quantity = decision.adjusted_quantity
        # full decision observability (3.0.1): reconstructable end-to-end
        self.repository.record_system_event("DECISION", {
            "ts": candle.timestamp.isoformat(),
            "symbol": candle.symbol, "instrument_key": instrument_key,
            "side": side, "quantity": quantity, "price": price,
            "strategy": self.strategy.name,
            "params_version": self.journal.params_version,
            "regime": self.regime, "triggers": triggers, "reason": reason,
            "allocated_capital": allocated_capital,
            "estimated_fees": estimated_fees,
            "router_decision": {
                "selected": bool((self.last_router_decision or {}).get("selected")),
                "strategy": (self.last_router_decision or {}).get("strategy"),
                "confidence": (self.last_router_decision or {}).get("confidence"),
            } if self.last_router_decision else None,
            "intent_id": intent_key,
        })
        try:
            self.broker.submit(side, quantity, price, intent_id=intent_key,
                               symbol=candle.symbol, instrument_key=instrument_key)
        except Exception as exc:
            self.repository.record_system_event(
                "SANDBOX_SUBMIT_FAILURE", {"error": type(exc).__name__,
                                           "side": side, "symbol": candle.symbol,
                                           "instrument_key": instrument_key,
                                           "ts": candle.timestamp.isoformat()}
            )
            logger.error("SANDBOX_SUBMIT_FAILURE", extra={"error": type(exc).__name__})
            return
        processed.add(intent_key)
        self.repository.set_engine_state("processed_signals", sorted(processed)[-2000:])
        self.submitted += 1
        if side == "BUY":
            if self.live_ops is not None:
                self.live_ops.record_submitted_entry(quantity=quantity, price=price)
            if self.daily is not None:
                self.daily.register_trade()
            else:
                self._trades_today += 1

    def reconcile(self) -> list[dict]:
        fills = self.broker.reconcile() if hasattr(self.broker, "reconcile") else []
        # L-ops liveness stamp: silence here is a silent 24/7 failure mode
        self.repository.set_engine_state(
            "last_reconcile_at", {"ts": datetime.now(timezone.utc).isoformat(),
                                  "fills": len(fills)})
        for fill in fills:
            if fill["side"] == "SELL":
                realized_delta = self.broker.ledger.realized_pnl - self._last_realized
                _order = self.broker.orders.get(fill.get("intent_id"))
                self.journal.record_outcome(
                    strategy=self.strategy.name,
                    symbol=(fill.get("symbol")
                            or (_order.symbol if _order is not None else None)
                            or "UNKNOWN"),  # honest: never substitute an instrument
                    entry=fill["price"], exit=fill["price"], quantity=fill["quantity"],
                    pnl=realized_delta,
                    fees=self.broker.ledger.fees, slippage=0.0,
                    duration_bars=self.processed, outcome="closed", regime=self.regime or "unknown",
                )
                self._last_realized = self.broker.ledger.realized_pnl
                if self.daily is not None:
                    self.daily.add_realized(realized_delta)
        if fills:
            self._persist_equity()
        return fills

    def _daily_snapshot(self, candle) -> None:
        from app.history.store import parse_local

        day = parse_local(candle.timestamp).date().isoformat()
        if day in self._snapshot_dates:
            return
        self._snapshot_dates.add(day)
        ledger = self.broker.ledger
        equity = ledger.equity()
        self.repository.upsert_daily_snapshot(
            day, equity=equity, realized=ledger.realized_pnl,
            unrealized=ledger.unrealized(), trades=self.submitted,
            regime=(self.regime_engine.current if self.regime_engine else self.regime))

    def ledger_mark(self, price: float) -> None:
        self.broker.ledger.mark = price
        self._persist_equity()

    def _persist_equity(self) -> None:
        equity = self.broker.ledger.equity()
        self._peak_equity = max(self._peak_equity, equity)
        drawdown = (self._peak_equity - equity) / self._peak_equity if self._peak_equity > 0 else 0.0
        from app.engine.state import equity_state_key
        self.repository.set_engine_state(
            equity_state_key(bool(getattr(self.broker, "LIVE", False))),
            {"equity": equity, "peak": self._peak_equity, "drawdown": drawdown,
             "ts": datetime.now(timezone.utc).isoformat()},
        )

    def _maybe_evaluate(self, force: bool = False) -> None:
        """Rolling-batch evaluation outside market hours (or at session end)."""
        if not force and (self.processed % self.evaluate_every != 0 or self.processed == 0):
            return
        if len(self._history) < 10:
            return
        from app.backtesting.costs import ExecutionConfig
        from app.backtesting.engine import BacktestEngine
        from app.learning.evaluator import evaluate_and_maybe_promote

        engine = BacktestEngine(ExecutionConfig())
        candidate = {"period": int(self.strategy.parameters.get("period", 2)) + 1}
        self.evaluation = evaluate_and_maybe_promote(
            self.repository, self._history, self.strategy.name,
            dict(self.strategy.parameters), engine, self.timeframe,
            candidate_parameters=candidate,
        )
        self.repository.record_system_event("EVALUATION_RUN", {
            "promoted": self.evaluation["promoted"],
            "version": self.evaluation.get("promoted_version"),
        })
        self.repository.record_learning_event("EVALUATION_RUN", {
            "promoted": self.evaluation["promoted"],
            "version": self.evaluation.get("promoted_version"),
            "batch_bars": len(self._history),
        })
        logger.info("EVALUATION_RUN", extra={"promoted": self.evaluation["promoted"]})

    def status(self) -> dict:
        return {
            "engine": "live" if getattr(self.broker, "LIVE", False) else "sandbox",
            "state": self.state,
            "mode": getattr(self.broker, "mode", "LOCAL_PAPER"),
            "live_trading": bool(getattr(self.broker, "LIVE", False)),
            "processed": self.processed,
            "submitted": self.submitted,
            "rejected_by_risk": self.rejected_by_risk,
            "signals": self.signals,
            "regime": self.regime,
            "strategy": {"name": self.strategy.name, "parameters": dict(self.strategy.parameters)},
            "ledger": self.broker.ledger.snapshot(),
            "broker": self.broker.status_view(),
            "evaluation": self.evaluation,
            "emergency_stop": bool((self.repository.get_engine_state("emergency_stop") or {}).get("stopped")),
            "promoted_version": (
                self.repository.promoted_version(self.strategy.name) if self.repository else None
            ),
            "daily_risk_state": self.daily.state() if self.daily else None,
            "router_decision": self.last_router_decision,
            "active_strategy_state": (
                self.repository.get_engine_state("active_strategy") or {}),
            "unresolved_intents": (
                len(self.intent_journal.unresolved()) if self.intent_journal else 0),
            "recovery": self._recovery_report,
            "live_ops": self.live_ops.status() if self.live_ops is not None else None,
            "engine_lock": (getattr(self, "live_lock", None).status()
                            if getattr(self, "live_lock", None) is not None else None),
        }


def classify_regime_safe(candles):
    from app.learning.regime import classify_regime

    try:
        return classify_regime(candles)
    except Exception:
        return "unknown"
