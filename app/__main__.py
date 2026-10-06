"""Paper-only CLI: `python -m app <command>` (V0.6.2).

Commands:
  upstox-test     Real authentication + connectivity test (needs UPSTOX/MARKET
                  DATA token in the environment; safe, read-only, no orders).
  market-stream   Connect and print normalized completed-candle market events
                  (plus market-status changes) from the real feed.
  paper-live      Full pipeline: Upstox → candles → strategies → risk →
                  PaperBroker (the existing `run.py --mode paper --source
                  upstox` implementation).
  paper-test      OFFLINE deterministic self-test: synthetic feed → normalized
                  MarketEvents → candles → strategies → risk → PaperBroker →
                  SQLite, with expected results (pure standard library).
  upstox-login    OAuth 2.0 authorization-code helper per the current official
                  documentation; stores the token in the gitignored .env.
  engine          24/7 engine: data → strategies → hard risk → SANDBOX orders
                  (explicit BROKER=UPSTOX_SANDBOX configuration required).
  sandbox-test    Verify SANDBOX credentials (read-only probe; --place-order
                  submits one small real sandbox order only on request).
  initialize      Idempotent first-run setup (directories, database,
                  instrument metadata; offline-safe).
  market-universe Instrument master sync/status/audit (official files).
  history         Historical candle sync/status/audit (V3 API, resumable).
  dataset         Versioned research datasets: build/audit.
  universe        Screen the eligible research universe (never trades it).
  research        Parameter experiments: run/status (OOS-gated).
  report          Human-readable weekly report (markdown/pdf).
  maintenance     Run due maintenance jobs once (scheduling-aware).

Real-money order routing exists but is OFF by default. It requires the
four-switch opt-in in Settings (MODE=LIVE + LIVE_TRADING=true +
BROKER=UPSTOX_LIVE + LIVE_CONFIRM) AND explicit non-zero hard risk limits;
the sandbox broker refuses live hosts. See LIVE_TRADING_ENABLEMENT.md.

Commands:
  live-preflight  LIVE readiness check (read-only; never places an order).
  live-status     live-ops snapshot from recorded state (read-only).
  live-halt       set the persisted EMERGENCY STOP (always permitted).
  live-resume     clear the persisted EMERGENCY STOP (armed sessions only).
"""

import argparse
import asyncio
import importlib
import json
from app.utils.clock import utc_today_iso
from pathlib import Path


def _load_settings():
    from app.config.settings import Settings

    return Settings.from_env()  # raises on unsafe MODE/LIVE_TRADING


def _provider_or_exit(settings):
    from app.data.factory import stream_provider

    try:
        return stream_provider(settings, "upstox")
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        raise SystemExit(2)


def cmd_upstox_test(args) -> int:
    """Credential verification BEFORE connecting, then auth+connectivity proof."""
    settings = _load_settings()
    provider = _provider_or_exit(settings)

    async def scenario():
        from app.data.upstox_auth import verify_feed_credential

        summary = {
            "command": "upstox-test",
            "ok": False,
            "credential_check": None,
            "authenticated": False,
            "websocket": "not_tried",
            "frames": 0,
            "candles": 0,
            "market_status": None,
            "provider": "upstox",
            "instrument_keys": provider.metrics()["instrument_keys"],
            "note": "read-only market data; orders remain simulated (PAPER ONLY)",
        }
        try:
            # V0.6.3: never assume the credential type — verify feed access
            # behaviorally BEFORE any WebSocket connection. Tokens are opaque;
            # only the documented GET authorize probe can prove feed access.
            check = await verify_feed_credential(
                settings.market_data_access_token,
                authorize_url=settings.market_data_authorize_url,
            )
            summary["credential_check"] = check.public_view()
            if check.verdict != "feed_ok":
                summary["note"] += "; credential NOT verified for feed V3 → connection NOT attempted"
                return summary
            agen = provider.stream(provider.symbols, provider.timeframe)
            try:
                bar = await asyncio.wait_for(anext(agen), timeout=args.timeout)
                summary["websocket"] = "connected"
                summary["candles"] = 1
                summary["first_completed_candle"] = bar.to_dict()
            except (asyncio.TimeoutError, StopAsyncIteration):
                summary["websocket"] = (
                    "connected_no_completed_candle_in_window"
                    if provider.status in {"CONNECTED", "STALE"}
                    else provider.status
                )
            metrics = provider.metrics()
            summary["authenticated"] = metrics["authenticated"]
            summary["market_status"] = metrics["market_status"]
            summary["frames"] = metrics["feed_counters"]["completed_candles"]
            summary["ok"] = summary["websocket"] == "connected"
        finally:
            await provider.close()
        return summary

    summary = asyncio.run(scenario())
    print(json.dumps(summary, indent=2, allow_nan=False))
    return 0 if summary["ok"] else 1


def cmd_market_stream(args) -> int:
    """Print normalized real market events (completed candles + market status)."""
    settings = _load_settings()
    provider = _provider_or_exit(settings)

    async def scenario():
        last_status = None

        def status_line():
            nonlocal last_status
            metrics = provider.metrics()
            status = metrics["market_status"]
            if status != last_status:
                last_status = status
                print(
                    json.dumps(
                        {"event": "MARKET_STATUS", "market_status": status,
                         "segments": metrics["segment_statuses"]},
                        allow_nan=False,
                    ),
                    flush=True,
                )

        async def consume():
            async for bar in provider.stream(provider.symbols, provider.timeframe):
                status_line()
                print(
                    json.dumps(
                        {"event": "CANDLE_COMPLETED", "candle": bar.to_dict()},
                        allow_nan=False,
                    ),
                    flush=True,
                )

        try:
            try:
                await asyncio.wait_for(consume(), timeout=args.duration)
            except (asyncio.TimeoutError, StopAsyncIteration):
                pass
            status_line()
        finally:
            await provider.close()

    asyncio.run(scenario())
    return 0


def _paper_test_continuation_scenario():
    """Deterministic restart continuation: OPEN status + last-tick duplicate +
    fresh declining ticks (bucket 7, 8) so the cursor replay is exercised."""
    from datetime import timedelta

    from app.data.market_events import MarketEvent
    from app.data.synthetic_feed import BASE_EPOCH, PROVIDER_NAME

    events = [
        MarketEvent(
            provider=PROVIDER_NAME, instrument_key="", kind="market_info",
            received_at=BASE_EPOCH + timedelta(seconds=7 * 60 + 5),
            market_status="NORMAL_OPEN",
            segment_statuses={"NSE": "NORMAL_OPEN"},
        )
    ]
    # Start at bucket 7: its completion re-produces the cursor candle (an exact
    # duplicate that must be ignored); bucket 8 is genuinely new. Replaying
    # older buckets would fail closed (documented out-of-order behavior).
    for bucket, price, quantity in ((7, 100.5, 10000), (8, 100.25, 10000)):
        stamp = BASE_EPOCH + timedelta(seconds=bucket * 60 + 10)
        events.append(MarketEvent(
            provider=PROVIDER_NAME, instrument_key="NSE_EQ|INE002A01018",
            kind="live", received_at=stamp, exchange_ts=stamp,
            ltp=price, last_traded_quantity=float(quantity),
        ))
    return events


# Deterministic expected results for the offline scenario (1m, momentum(2, 0),
# default ExecutionConfig). The scenario fixes every price/quantity/timestamp,
# so these values are stable across runs and platforms; the pinned numbers are
# verified by tests/test_paper_test_v064.py.
EXPECTED_CLOSES = [100.5, 101.0, 101.5, 102.0, 101.0, 100.75]
EXPECTED_REALIZED_PNL = -362.41536614602916  # includes fees; deterministic
EXPECTED_EQUITY = 99637.58463385397  # 100000 + realized P&L (flat position)


def cmd_paper_test(args) -> int:
    """Offline deterministic end-to-end paper-pipeline proof (V0.6.4).

    Synthetic feed → normalized MarketEvent → shared candle pipeline →
    market-status gate → strategy → risk → PaperBroker → SQLite. Pure
    standard library: runs without fastapi/pydantic/httpx/websockets/protobuf
    (e.g. Android Termux). Never touches a real provider and never touches
    your trading database unless --database is given explicitly.
    """
    import math
    import os
    import tempfile
    from pathlib import Path as _Path

    from app.backtesting.costs import ExecutionConfig
    from app.config.settings import Settings
    from app.data.synthetic_feed import PROVIDER_NAME, SyntheticStream
    from app.database.database import Database
    from app.database.repository import Repository
    from app.paper_trading.broker import PaperBroker
    from app.paper_trading.simulator import PaperSimulator
    from app.strategies import create_strategy

    timeframe = "1m"
    settings = Settings.from_env()  # MODE=PAPER / LIVE_TRADING=false enforced
    config = ExecutionConfig(**settings.execution_kwargs())
    strategy = create_strategy("momentum", {"period": 2, "threshold": 0.0})
    owned_temp = False
    if args.database:
        database_path = args.database
    else:
        handle, database_path = tempfile.mkstemp(prefix="paper_test_", suffix=".db")
        os.close(handle)
        _Path(database_path).unlink(missing_ok=True)  # Database creates its own file
        owned_temp = True

    def scrub_ids(payload):
        """Canonical economics view (order/fill/trade IDs are random UUIDs)."""
        if isinstance(payload, dict):
            return {
                key: scrub_ids(value)
                for key, value in payload.items()
                if key not in {"id", "order_id"}
            }
        if isinstance(payload, list):
            return [scrub_ids(item) for item in payload]
        return payload

    async def full_run(path):
        db = Database(f"sqlite:///{path}")
        repo = Repository(db)
        broker = PaperBroker(config, timeframe=timeframe, repository=repo)
        sim = PaperSimulator(broker)
        provider = SyntheticStream(timeframe)
        await sim.start(provider, strategy, provider.symbols)
        await sim.task
        result = {
            "state": sim.status()["state"],
            "status": sim.status(),
            "account": broker.get_balance(),
            "orders": broker.get_orders(),
            "fills": len(broker.fills),
            "trades": broker.portfolio.trades,
            "processed": sim.processed,
        }
        broker.close()
        db.close()
        return result

    async def collect_candles():
        provider = SyntheticStream(timeframe)
        collected = []
        async for bar in provider.stream(provider.symbols, timeframe):
            collected.append(bar.to_dict())
        metrics = provider.metrics()
        await provider.close()
        return collected, metrics

    async def restart_run(path):
        """Restore the persisted account; replay-last + continue deterministically."""
        db = Database(f"sqlite:///{path}")
        repo = Repository(db)
        broker = PaperBroker(config, timeframe=timeframe, repository=repo)  # restore
        sim = PaperSimulator(broker)
        provider = SyntheticStream(timeframe, scenario=_paper_test_continuation_scenario())
        await sim.start(provider, strategy, provider.symbols)
        await sim.task
        result = {
            "state": sim.status()["state"],
            "account": broker.get_balance(),
            "orders": broker.get_orders(),
            "fills": len(broker.fills),
            "processed": sim.processed,
        }
        broker.close()
        db.close()
        return result

    async def scenario():
        collected, feed_metrics = await collect_candles()
        first = await full_run(database_path)
        restart = await restart_run(database_path)
        second_db = database_path + ".determinism"
        second = await full_run(second_db)
        return collected, feed_metrics, first, restart, second

    collected, feed_metrics, first, restart, second = asyncio.run(scenario())
    account = first["account"]
    orders = first["orders"]
    equity = account["equity"]
    realized = account["realized_pnl"]
    sides = [order["side"] for order in orders]
    order_ids = [order["id"] for order in orders]
    economics = ("account", "orders", "fills", "trades", "processed")
    checks = {
        "pipeline_completed_STOPPED": first["state"] == "STOPPED",
        "market_status_gate_held_closed_candle": (
            feed_metrics["suppressed_by_market_status"] >= 1
            and feed_metrics["market_open"] is True
        ),
        "duplicate_tick_counted_not_executed": (
            feed_metrics["feed_counters"]["duplicates"] >= 1
        ),
        "exactly_two_orders_buy_then_sell": sides == ["BUY", "SELL"],
        "no_duplicate_order_ids": len(set(order_ids)) == len(order_ids) == 2,
        "two_fills_one_closed_trade": first["fills"] == 2 and len(first["trades"]) == 1,
        "position_flat_after_round_trip": not account["positions"],
        "realized_pnl_matches_trade_record": bool(first["trades"]) and math.isclose(
            realized, first["trades"][0]["pnl"], rel_tol=0, abs_tol=1e-9
        ),
        # With a flat position, equity must equal capital + realized P&L
        # (realized P&L is net of fees in this accounting model).
        "equity_is_capital_plus_realized_when_flat": (
            not account["positions"]
            and math.isclose(
                equity, config.initial_capital + realized, rel_tol=0, abs_tol=1e-9
            )
        ),
        "expected_closes_exact": [candle["close"] for candle in collected] == EXPECTED_CLOSES,
        "restart_restored_equity_identical": math.isclose(
            restart["account"]["equity"], equity, rel_tol=0, abs_tol=1e-9
        ),
        "restart_added_no_orders": len(restart["orders"]) == 2,
        "restart_replay_duplicate_suppressed": restart["processed"] == 2,
        "independent_run_economics_identical": scrub_ids(
            {k: second[k] for k in economics}
        ) == scrub_ids({k: first[k] for k in economics}),
        "second_run_equity_exact": math.isclose(
            second["account"]["equity"], equity, rel_tol=0, abs_tol=1e-9
        ),
    }
    checks["expected_equity_exact"] = math.isclose(
        equity, EXPECTED_EQUITY, rel_tol=0, abs_tol=1e-9
    )
    checks["expected_realized_pnl_exact"] = math.isclose(
        realized, EXPECTED_REALIZED_PNL, rel_tol=0, abs_tol=1e-9
    )
    report = {
        "command": "paper-test",
        "scope": f"OFFLINE SYNTHETIC VERIFICATION (provider={PROVIDER_NAME}); NOT real Upstox data",
        "provider": PROVIDER_NAME,
        "instrument_key": "NSE_EQ|INE002A01018",
        "timeframe": timeframe,
        "strategy": {"name": "momentum", "parameters": {"period": 2, "threshold": 0.0}},
        "expected": {
            "closes": EXPECTED_CLOSES,
            "orders": 2, "fills": 2, "closed_trades": 1, "position": "flat",
            "suppressed_by_market_status_at_least": 1,
            "duplicate_ticks_at_least": 1,
            "restart": {"new_orders": 0, "equity_change": 0.0, "processed": 2},
        },
        "actual": {
            "final_equity": equity,
            "realized_pnl": realized,
            "fees": account["fees"],
            "closes": [candle["close"] for candle in collected],
            "orders": sides,
            "fills": first["fills"],
            "closed_trades": len(first["trades"]),
            "trade": first["trades"][0] if first["trades"] else None,
            "suppressed_by_market_status": feed_metrics["suppressed_by_market_status"],
            "duplicate_ticks": feed_metrics["feed_counters"]["duplicates"],
            "processed_first_run": first["processed"],
            "processed_restart_run": restart["processed"],
            "risk_counters": first["status"]["risk_counters"],
        },
        "checks": checks,
        "ok": all(checks.values()),
        "paper_only": {"mode": "PAPER", "live_trading": False},
    }
    if owned_temp:
        for suffix in ("", "-shm", "-wal", ".paper.lock", ".determinism"):
            _Path(database_path + suffix).unlink(missing_ok=True)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["ok"] else 1


def cmd_paper_live(args) -> int:
    """Full paper pipeline over the real data source (existing run.py engine).

    Extra arguments land in ``args.passthrough`` (the positional ``nargs="*"``
    after ``--``) and are forwarded to run.py verbatim.
    """
    from run import main as run_main

    # BUGFIX: this read a global ``argv`` that does not exist, so any use of
    # the passthrough form died with NameError: name 'argv' is not defined.
    # ``args.passthrough`` is already the list of extra arguments.
    passthrough = list(args.passthrough or [])
    # Surface a bad instrument key as guidance rather than a raw ValueError
    # traceback out of the stream factory (DEFAULT_SYMBOL=DEMO, the shipped
    # default, is not an Upstox instrument key).
    settings = _load_settings()
    from app.data.upstox_stream import from_symbol

    try:
        from_symbol(settings.default_symbol)
    except ValueError:
        print(json.dumps({
            "ok": False,
            "error": f"DEFAULT_SYMBOL={settings.default_symbol!r} is not an "
                     "Upstox instrument key",
            "hint": "paper-live streams real Upstox data, so it needs a real "
                    "instrument. Set DEFAULT_SYMBOL=NSE_EQ|INE002A01018 (or use "
                    "the paper/replay path: python run.py --mode paper "
                    "--source replay)",
        }, indent=2))
        return 2
    run_main(["--mode", "paper", "--source", "upstox", *passthrough])
    return 0


def cmd_engine(args) -> int:
    """24/7 engine: Upstox V3 data → strategies → hard risk → order routing.

    Requires explicit configuration: BROKER=UPSTOX_SANDBOX (sandbox orders) or
    BROKER=UPSTOX_LIVE (REAL money; requires the full four-switch opt-in plus
    non-zero hard risk limits in Settings), plus DATA_SOURCE=upstox.
    LOCAL_PAPER users keep the existing paper pipeline
    (`paper-live`/run.py), which this command refuses to replace.
    """
    settings = _load_settings()
    broker_mode = settings.broker.upper()
    if broker_mode not in {"UPSTOX_SANDBOX", "UPSTOX_LIVE"}:
        print(json.dumps({
            "ok": False,
            "error": "engine requires explicit BROKER=UPSTOX_SANDBOX or "
                     "BROKER=UPSTOX_LIVE configuration; LOCAL_PAPER continues "
                     "via 'python -m app paper-live'",
        }, indent=2))
        return 2
    if (settings.data_source != "upstox"):
        print(json.dumps({"ok": False,
                          "error": "engine requires DATA_SOURCE=upstox (real market data)"},
                         indent=2))
        return 2
    # A live engine is intraday-only; refuse before building anything, rather
    # than silently downgrading the engine while the stream keeps its timeframe.
    from app.data.factory import live_timeframe_error, market_data_mode_error

    for error in (live_timeframe_error(settings, broker_mode),
                  market_data_mode_error(settings)):
        if error:
            print(json.dumps({"ok": False, "error": error}, indent=2))
            return 2

    from app.backtesting.costs import ExecutionConfig
    from app.brokers.live import UpstoxLiveBroker
    from app.brokers.sandbox import UpstoxSandboxBroker
    from app.data.factory import engine_timeframe
    from app.database.database import Database
    from app.database.repository import Repository
    from app.engine.sandbox_engine import SandboxEngine
    from app.learning.evaluator import evaluate_and_maybe_promote  # noqa: F401 (import guard)
    from app.learning.journal import LearningJournal
    from app.risk.limits import HardRiskLimits
    from app.strategies import create_strategy

    provider = _provider_or_exit(settings)
    database = Database(settings.database_url)
    repository = Repository(database)
    initial_capital = ExecutionConfig(**settings.execution_kwargs()).initial_capital
    if broker_mode == "UPSTOX_LIVE":
        # Settings has already proven the four locks + non-zero risk limits.
        # The explicit confirm= below is the fifth, in-code barrier.
        broker = UpstoxLiveBroker(
            settings.upstox_live_access_token,
            provider._keys[0],
            confirm=settings.live_confirm,
            base_url=settings.upstox_live_base_url,
            history_url=settings.upstox_live_order_history_url,
            product=settings.live_product,
            tag=settings.live_order_tag,
            repository=repository,
            initial_capital=initial_capital,
            algo_name=settings.live_algo_name,
        )
        print(json.dumps({
            "ok": True,
            "mode": "LIVE — REAL MONEY",
            "broker": broker.mode,
            "order_host": broker._base_url,
            "product": settings.live_product,
            "hard_risk_limits": {
                "max_position_value": settings.max_position_value,
                "max_open_positions": settings.max_open_positions,
                "max_daily_loss": settings.max_daily_loss,
                "max_trades_per_day": settings.max_trades_per_day,
            },
            "note": "orders are real; EMERGENCY STOP / Ctrl-C reconciles and stops",
        }, indent=2), flush=True)
    else:
        broker = UpstoxSandboxBroker(
            settings.upstox_sandbox_access_token,
            provider._keys[0],
            base_url=settings.upstox_sandbox_base_url,
            history_url=settings.upstox_sandbox_order_history_url,
            product=settings.sandbox_product,
            tag=settings.sandbox_order_tag,
            repository=repository,
            initial_capital=initial_capital,
        )
    from app.engine.adaptive import RouterStrategySource
    from app.engine.events import EventDrivenDecisionEngine, PauseGate
    from app.learning.calibration import ConfidenceCalibration
    from app.maintenance.final_jobs import build_maintenance_jobs
    from app.maintenance.scheduler import MaintenanceScheduler

    # V2.1 L-ops: safety rails for real-money sessions. Built only for the live
    # broker so paper/sandbox behaviour is untouched; the engine treats
    # live_ops=None as "no operational layer".
    live_ops = None
    if broker_mode == "UPSTOX_LIVE":
        from app.live.ops import LiveOperations

        live_ops = LiveOperations.from_settings(settings, repository, broker)
        ops_status = live_ops.status()
        from app.live.alerts import recent_alerts

        print(json.dumps({
            "ok": True,
            "live_ops": {
                "canary": ops_status["canary"],
                "token": ops_status["token"]["decision"],
                "breaches": ops_status["breaches"],
                "alerts": ops_status["alerts"],
                "lock": ops_status.get("lock"),
                "unacknowledged_critical": sum(
                    1 for alert in recent_alerts(repository, limit=200)
                    if alert["severity"] == "CRITICAL"
                    and not alert["acknowledged"]),
            },
        }, indent=2, default=str), flush=True)

    # FINAL-11/10 §3–§6: in live mode the engine NEVER holds the live broker.
    # It receives a facade whose only submission route is the
    # LiveExecutionGateway (intent → epoch → session state → emergency-stop
    # semantics → phrase → broker identity → guard chain → durable audit).
    execution_broker = broker
    if broker_mode == "UPSTOX_LIVE":
        from app.live.gateway import LiveExecutionGateway
        from app.live.gateway_broker import GatewayRoutedBroker
        from app.live.guard_chain import default_chain
        from app.live.ops import read_emergency_stop
        from app.live.session import LiveSessionManager

        session_manager = LiveSessionManager(repository)

        def _live_risk_context():
            """Fresh risk picture for every submission; any error inside the
            gateway's wrapper turns this into a fail-closed refusal."""
            stopped, _why = read_emergency_stop(repository)
            snap = broker.ledger.snapshot()
            exposure = float(snap.get("position_quantity", 0.0) or 0.0) * float(
                snap.get("mark") or snap.get("position_avg_price") or 0.0)
            realized = float(snap.get("realized_pnl", 0.0) or 0.0)
            daily_limit = float(settings.max_daily_loss or 0.0)
            equity = float(snap.get("equity") or 0.0)
            peak = float(repository.get_engine_state("peak_equity", 0.0) or 0.0)
            dd_limit = float(getattr(settings, "live_max_equity_drawdown", 0.0)
                             or 0.0)
            return {
                "emergency_stop": bool(stopped),
                "current_exposure": exposure,
                "daily_loss_breached": bool(
                    daily_limit and realized <= -daily_limit),
                "drawdown_breached": bool(
                    dd_limit and peak and equity
                    and (peak - equity) / peak >= dd_limit),
                # The canary ramp is enforced upstream by live_ops.entry_guard
                # (which blocks or reduces each ramped entry); the chain flag
                # stays False unless an external process raises it.
                "canary_exhausted": False,
            }

        gateway = LiveExecutionGateway(
            settings=settings, repository=repository,
            session_manager=session_manager, broker=broker,
            guard_chain=default_chain(
                settings=settings,
                max_entry_age_s=float(getattr(settings, "live_max_entry_age_s",
                                              30.0) or 30.0)),
            epochs=session_manager.epochs,
            emergency_stop_reader=read_emergency_stop,
            context_provider=_live_risk_context,
            account_scope=getattr(broker, "account_scope", ""),
            live_ops=live_ops)
        first_key = (getattr(provider, "_keys", None) or [""])[0]
        execution_broker = GatewayRoutedBroker(
            broker, gateway,
            market_id=str(first_key).split("|")[0] or "UNKNOWN",
            strategy_id="engine",
            account_scope=getattr(broker, "account_scope", ""),
            universe=str(getattr(settings, "market_universe", "") or ""))
        print(json.dumps({
            "ok": True,
            "execution_gateway": {
                "enabled": True,
                "session_state": session_manager.current().state,
                "epoch": (session_manager.epochs.current() or {}).get("epoch"),
                "epoch_valid": bool((session_manager.epochs.current() or {})
                                    .get("valid")),
                "note": "every live order is authorized by the execution "
                        "gateway before it can reach the broker",
            },
        }, indent=2, default=str), flush=True)

    event_engine = EventDrivenDecisionEngine(
        move_threshold=settings.event_move_threshold,
        volume_spike=settings.event_volume_spike)
    # 3.0.3: the engine registers the ACTIVE strategy's cadence at
    # construction and on every router strategy swap — the normal cadence is
    # never bound to a hard-coded strategy name here.
    pause_gate = PauseGate(repository)
    # 3.0.1: the router (promoted, positive-OOS candidates) drives execution
    strategy_source = RouterStrategySource(
        repository, calibration=ConfidenceCalibration(repository))
    # symbol → authoritative instrument key map from the provider
    key_to_symbol = getattr(provider, "_symbol_for", {}) or {}
    instrument_keys = {symbol: key for key, symbol in key_to_symbol.items()}
    if not instrument_keys:
        instrument_keys = {key.split("|")[-1]: key
                           for key in getattr(provider, "_keys", [])}
    scheduler = MaintenanceScheduler(build_maintenance_jobs(
        repository, settings, settings.database_url,
        backup_dir=str(Path(settings.data_dir) / "backups")))
    engine = SandboxEngine(
        provider=provider,
        strategy=create_strategy("momentum", {"period": 2, "threshold": 0.0}),
        broker=execution_broker,
        repository=repository,
        risk_limits=HardRiskLimits.from_settings(settings),
        journal=LearningJournal(repository),
        timeframe=engine_timeframe(settings),
        event_engine=event_engine,
        pause_gate=pause_gate,
        maintenance_scheduler=scheduler,
        strategy_source=strategy_source,
        live_ops=live_ops,
        instrument_keys=instrument_keys,
        market_timezone=settings.market_timezone,
        stop_loss=float(settings.stop_loss or 0.0),
        take_profit=float(settings.take_profit or 0.0),
    )
    if broker_mode == "UPSTOX_LIVE":
        # Live intents must carry the strategy that actually decided them;
        # the router can swap strategies at runtime, so resolve at submit.
        execution_broker._strategy_id = lambda: getattr(
            getattr(engine, "strategy", None), "name", "engine")
    try:
        status = engine.run(max_candles=args.max_candles)
        print(json.dumps(status, indent=2, allow_nan=False, default=str))
        return 0 if status["state"] in {"RUNNING", "STOPPED"} else 1
    except KeyboardInterrupt:
        engine.reconcile()
        print(json.dumps({"state": "STOPPED", "reason": "interrupt"}, indent=2))
        return 0
    finally:
        database.close()


def _open_ops_repository(settings):
    """Open the configured database for an ops command (caller must close)."""
    from app.database.database import Database
    from app.database.repository import Repository

    database = Database(settings.database_url)
    return database, Repository(database)


def cmd_live_halt(args) -> int:
    """Set the persisted EMERGENCY STOP from the terminal.

    Deliberately always permitted: halting is the safe direction, so it must
    work even when configuration is broken, the engine is down, or the caller
    has no live opt-in. It does NOT cancel broker-side orders (the engine does
    that when it is running); it stops this system from opening anything new.
    """
    import os
    from datetime import datetime, timezone

    payload: dict = {"scope": "LIVE HALT (persisted emergency stop)"}
    settings, config_error = None, ""
    try:
        settings = _load_settings()
    except ValueError as exc:
        # Halting is the safe direction: a rejected configuration must not be
        # able to prevent a halt. Fall back to the raw environment so the one
        # thing we still need (where the state lives) is available. Settings
        # has already run load_dotenv by this point, so .env values are set.
        config_error = f"configuration rejected: {exc}"
        payload.update(configuration_rejected=True, config_reason=config_error,
                       database_url_source="raw DATABASE_URL (Settings rejected)")
        database_url = os.environ.get("DATABASE_URL", "").strip() \
            or "sqlite:///trading_bot.db"
        database, repository = None, None
        try:
            from app.database.database import Database
            from app.database.repository import Repository

            database = Database(database_url)
            repository = Repository(database)
        except Exception as exc:  # pragma: no cover - only a broken path lands here
            payload.update(ok=False, halted=False,
                           reason=f"{config_error}; and the state database "
                                  f"({database_url}) could not be opened: "
                                  f"{type(exc).__name__}")
            print(json.dumps(payload, indent=2))
            return 2
    else:
        try:
            database, repository = _open_ops_repository(settings)
        except Exception as exc:
            payload.update(ok=False, halted=False,
                           reason=f"cannot open DATABASE_URL: {type(exc).__name__}")
            print(json.dumps(payload, indent=2))
            return 2
    try:
        already = bool((repository.get_engine_state("emergency_stop") or {})
                       .get("stopped"))
        halt_reason = args.reason or "manual halt via CLI"
        repository.set_engine_state("emergency_stop", {
            "stopped": True, "ts": datetime.now(timezone.utc).isoformat(),
            "reason": halt_reason})
        repository.record_system_event("EMERGENCY_STOP", {
            "reason": halt_reason, "source": "CLI"})
        session_blocked = False
        try:
            from app.live.session import (ACTIVE, ARMED, PAUSED, RECONCILING,
                                          LiveSessionManager)

            manager = LiveSessionManager(repository)
            if manager.current().state in (ACTIVE, ARMED, PAUSED, RECONCILING):
                manager.block(reason=f"emergency stop: {halt_reason}")
                session_blocked = True
        except Exception as exc:
            # The persisted kill flag is already set; the running engine and
            # final gateway both independently fail closed on it.
            payload["session_block_error_type"] = type(exc).__name__
        payload.update(ok=True, halted=True, was_already_halted=already,
                       live_session_blocked=session_blocked,
                       reason=halt_reason,
                       note="the running engine cancels working orders when it "
                            "observes the stop and remains available for "
                            "risk-reducing exits; positions are NOT auto-closed")
        if config_error:
            payload["reason"] = f"{args.reason or 'manual halt via CLI'} " \
                                f"(while: {config_error})"
    finally:
        database.close()
    print(json.dumps(payload, indent=2))
    return 0


def cmd_live_resume(args) -> int:
    """Clear the persisted emergency stop. Requires the live opt-in to be valid.

    Resuming is the dangerous direction, so unlike ``live-halt`` this refuses to
    act on an invalid configuration — a half-configured .env must not be able to
    re-arm trading.
    """
    payload: dict = {"scope": "LIVE RESUME (clear persisted emergency stop)"}
    from datetime import datetime, timezone

    try:
        settings = _load_settings()
    except ValueError as exc:
        payload.update(ok=False, resumed=False,
                       reason=f"configuration rejected; refusing to resume: {exc}")
        print(json.dumps(payload, indent=2))
        return 2
    if not args.allow_non_live and not (
            settings.broker.upper() == "UPSTOX_LIVE"
            and bool(settings.live_trading)):
        payload.update(
            ok=False, resumed=False,
            reason="BROKER is not an armed UPSTOX_LIVE session; pass "
                   "--allow-non-live only if you intentionally cleared the live "
                   "configuration")
        print(json.dumps(payload, indent=2))
        return 2
    database, repository = _open_ops_repository(settings)
    try:
        was = bool((repository.get_engine_state("emergency_stop") or {}).get("stopped"))
        from app.live.session import (ACTIVE, ARMED, PAUSED, RECONCILING,
                                      BLOCKED, LiveSessionManager)

        manager = LiveSessionManager(repository)
        current = manager.current()
        was_live_session = current.state in {
            ACTIVE, ARMED, PAUSED, RECONCILING, BLOCKED}
        if current.state in (ACTIVE, ARMED, PAUSED, RECONCILING):
            # Invalidate authorization BEFORE clearing the persisted flag.
            manager.block(reason="emergency stop clear requires a fresh ARM")
        repository.set_engine_state("emergency_stop", {
            "stopped": False,
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": "cleared by operator; fresh ARM/ACTIVATE required"})
        repository.record_system_event(
            "ENGINE_RESUME", {"source": "CLI", "fresh_arm_required": True})
        released = False
        if (was_live_session and manager.current().state == BLOCKED
                and "emergency stop" in manager.current().reason.lower()):
            manager.release_block(
                destination="STOPPED",
                reason="emergency stop cleared; fresh ARM and ACTIVATE required")
            released = True
        payload.update(ok=True, resumed=True, was_halted=was,
                       live_session_stopped=(manager.current().state == "STOPPED"
                                             if was_live_session else False),
                       fresh_arm_required=was_live_session or released,
                       note="the emergency flag is clear, but any prior live "
                            "authorization remains invalid; a fresh ARM and "
                            "ACTIVATE are required")
    finally:
        database.close()
    print(json.dumps(payload, indent=2))
    return 0


def cmd_live_digest(args) -> int:
    """Print (and optionally post) a read-only roll-up of recorded state."""
    from app.live.alerts import AlertRouter
    from app.live.digest import build_digest, digest_markdown, post_digest

    settings = _load_settings()
    database, repository = _open_ops_repository(settings)
    try:
        live = (settings.broker.upper() == "UPSTOX_LIVE")
        digest = build_digest(repository, hours=args.hours, live=live)
        if args.post:
            digest["post"] = post_digest(
                AlertRouter(repository, webhook_url=settings.alert_webhook_url),
                digest)
        output = (digest_markdown(digest) if args.markdown
                  else json.dumps(digest, indent=2, default=str))
    finally:
        database.close()
    print(output)
    if args.out:
        from pathlib import Path as _Path

        # encoding is NOT optional here. The digest contains characters outside
        # every legacy code page (the "→" in the window line is one), and a
        # default-encoding write on Windows raises UnicodeEncodeError, leaving
        # an empty file behind. `ensure_utf8_output` fixes the console, not
        # files.
        _Path(args.out).write_text(
            digest_markdown(digest) if args.markdown else
            json.dumps(digest, indent=2, default=str),
            encoding="utf-8")
    return 0


def cmd_live_drill(args) -> int:
    """Exercise every L-ops rail offline and report the recorded evidence.

    Nothing here places, modifies or cancels an order and nothing touches the
    network. Run it after changing the rails, before trusting them.
    """
    from app.live.drill import run_drill

    report = run_drill(database_url=args.database_url or None)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("ok") else 1


def _lock_status(settings, repository) -> dict:
    """The single-engine lock view for operators (read-only)."""
    from app.live.lock import EngineLock

    return EngineLock(repository,
                      stale_after_seconds=settings.live_lock_stale_seconds,
                      live=settings.broker.upper() == "UPSTOX_LIVE").status()


def cmd_live_ack(args) -> int:
    """Acknowledge alerts so an unattended CRITICAL is visible as such.

    Acknowledgement changes only local alert bookkeeping: it cannot place,
    cancel or modify an order and it cannot change a risk limit.
    """
    from app.live.alerts import AlertAcknowledgements, recent_alerts

    settings = _load_settings()
    database, repository = _open_ops_repository(settings)
    try:
        acks = AlertAcknowledgements(repository)
        by = args.by or "operator"
        if args.id:
            result = acks.acknowledge_many(args.id, by=by, note=args.note)
        elif args.kind:
            result = acks.acknowledge_kind(args.kind, by=by, note=args.note)
        elif args.all:
            ids = [alert["id"] for alert in recent_alerts(repository, limit=500)
                   if alert.get("id")]
            result = acks.acknowledge_many(ids, by=by, note=args.note)
        else:
            outstanding = acks.outstanding_critical(
                older_than_minutes=0, limit=500)
            result = {"ok": True, "outstanding_critical": outstanding,
                      "hint": "pass --id <id> (repeatable), --kind <KIND> or --all"}
        payload = {"scope": "LIVE ACK (alert acknowledgement)", **result}
        payload["status"] = acks.status()
    finally:
        database.close()
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload.get("ok") else 1


def cmd_live_status(args) -> int:
    """Print the live-operations snapshot from RECORDED state (read-only).

    Works while the engine is stopped or crashed — that is exactly when an
    operator needs it — because everything is read from the database.
    """
    from app.config.hosts import LIVE_ORDER_HOSTS, host_of
    from app.live.alerts import recent_alerts
    from app.live.guards import CanaryGuard, TokenGuard
    from app.live.watchdog import BreachMonitor, LiveWatchdog

    settings = _load_settings()
    database, repository = _open_ops_repository(settings)
    try:
        broker_mode = settings.broker.upper()
        live = bool(settings.live_trading) and broker_mode == "UPSTOX_LIVE"
        token = TokenGuard(repository,
                           max_age_hours=settings.live_token_max_age_hours,
                           warn_minutes=settings.live_token_warn_minutes)
        canary = CanaryGuard(repository,
                             max_notional=settings.live_canary_max_notional,
                             first_orders=settings.live_canary_orders)
        watchdog = LiveWatchdog(repository, token_guard=token,
                                stuck_order_minutes=settings.live_stuck_order_minutes,
                                reconcile_silence_minutes=(
                                    settings.live_reconcile_silence_minutes))
        equity = repository.get_engine_state("live_equity") or {}
        payload = {
            "scope": "LIVE STATUS (read-only)",
            "mode": settings.mode.upper(),
            "live_trading": live,
            "broker": broker_mode,
            "order_host": settings.upstox_live_base_url if live else None,
            "order_host_is_documented_live": (
                host_of(settings.upstox_live_base_url) in LIVE_ORDER_HOSTS
                if live else None),
            "emergency_stop": bool(
                (repository.get_engine_state("emergency_stop") or {}).get("stopped")),
            "last_reconcile_at": repository.get_engine_state("last_reconcile_at"),
            "open_orders": (len(repository.open_live_orders()) if live
                            else len(repository.open_sandbox_orders())),
            "token": token.status(),
            "canary": canary.status(),
            "breaches": BreachMonitor(
                repository, max_daily_loss=settings.max_daily_loss,
                max_equity_drawdown=settings.live_max_equity_drawdown,
                auto_stop=settings.live_auto_stop_on_breach).status(),
            "equity": equity,
            "findings": [f.to_dict() for f in watchdog.inspect()],
            "alerts": recent_alerts(repository, limit=10),
            "lock": _lock_status(settings, repository),
            "unacknowledged_critical": sum(
                1 for alert in recent_alerts(repository, limit=200)
                if alert["severity"] == "CRITICAL" and not alert["acknowledged"]),
            "pending_alert_retries": len(
                repository.get_engine_state("alert_pending_deliveries", []) or []),
        }
        if args.audit:
            # Report BOTH trails: an operator auditing a discrepancy should not
            # have to remember which mode is currently configured.
            from app.live.pnl import replay_fills

            payload["audit_replay"] = {
                "live": replay_fills(repository, live=True).to_dict(),
                "sandbox": replay_fills(repository, live=False).to_dict(),
            }
    finally:
        database.close()
    print(json.dumps(payload, indent=2, default=str))
    return 0


def cmd_live_preflight(args) -> int:
    """LIVE readiness check — READ-ONLY: places no order, ever.

    Prints the resolved order host, the four locks, the hard risk limits and a
    static-IP/regulatory reminder, then probes the documented order-history
    endpoint to classify the credential. Refuses to run at all unless Settings
    accepted BROKER=UPSTOX_LIVE, so a misconfigured environment reports exactly
    which switches are missing instead of half-starting a live engine.
    """
    import os

    payload: dict = {"scope": "LIVE PREFLIGHT (read-only; no order placed)"}
    try:
        settings = _load_settings()
    except ValueError as exc:
        payload.update(ok=False, performed=False,
                       reason=f"configuration rejected before any request: {exc}")
        print(json.dumps(payload, indent=2))
        return 2
    if settings.broker.upper() != "UPSTOX_LIVE":
        payload.update(ok=False, performed=False,
                       reason="BROKER is not UPSTOX_LIVE; live preflight not applicable",
                       broker=settings.broker.upper())
        print(json.dumps(payload, indent=2))
        return 2

    from app.brokers.live import UpstoxLiveBroker
    from app.brokers.sandbox import SandboxAuthError, SandboxRequestError, \
        classify_history_probe
    from app.config.hosts import LIVE_ORDER_HOSTS, host_of

    token = settings.upstox_live_access_token or os.getenv(
        "UPSTOX_LIVE_ACCESS_TOKEN", "")
    payload.update(
        ok=True, performed=True,
        broker=settings.broker.upper(),
        mode=settings.mode.upper(),
        live_trading=bool(settings.live_trading),
        order_host=settings.upstox_live_base_url,
        order_host_is_documented_live=host_of(settings.upstox_live_base_url)
        in LIVE_ORDER_HOSTS,
        history_host=settings.upstox_live_order_history_url,
        database_url=settings.database_url,
        hard_risk_limits={
            "max_position_value": settings.max_position_value,
            "max_open_positions": settings.max_open_positions,
            "max_daily_loss": settings.max_daily_loss,
            "max_trades_per_day": settings.max_trades_per_day,
        },
    )
    # V2.1: the same checks the engine and the API rely on, run offline.
    from app.config.hosts import host_of as _host_of
    from app.live.preflight import run_checks, summarize

    # Settings has already accepted the live opt-in, so the checks below are
    # the operational-readiness layer (canary, drawdown cap, alerts, freshness).
    repository = None
    database = None
    try:
        from app.database.database import Database
        from app.database.repository import Repository

        database = Database(settings.database_url)
        repository = Repository(database)
        report = summarize(run_checks(settings, repository=repository))
    except Exception as exc:  # a broken DB must not masquerade as "ready"
        report = {"ok": False, "errors": [
            {"name": "database", "severity": "ERROR",
             "detail": f"could not open DATABASE_URL: {type(exc).__name__}"}],
            "warnings": [], "checks": []}
    payload["readiness"] = {
        "ok": report["ok"],
        "errors": [c["detail"] for c in report["errors"]],
        "warnings": [c["detail"] for c in report["warnings"]],
        "checks": report["checks"],
    }
    payload["order_host_is_documented_live"] = _host_of(
        settings.upstox_live_base_url) in LIVE_ORDER_HOSTS
    if not token.strip():
        payload.update(credential_checked=False, reason="no live token configured")
        print(json.dumps(payload, indent=2))
        return 3
    try:
        broker = UpstoxLiveBroker(
            token, args.instrument, confirm=settings.live_confirm,
            base_url=settings.upstox_live_base_url,
            history_url=settings.upstox_live_order_history_url,
            product=settings.live_product, tag=settings.live_order_tag,
            algo_name=settings.live_algo_name,
        )
        response = broker._request(
            "GET", settings.upstox_live_order_history_url,
            params={"order_id": "0"})
        verdict = classify_history_probe(response)
        payload.update(
            credential_checked=True,
            credential_accepted=verdict["credential_accepted"],
            probe=f"HTTP {verdict['http_status']} {verdict['class']} "
                  f"codes={verdict.get('codes', [])}",
        )
        if verdict["class"] == "unexpected":
            payload["unexpected_probe_response"] = True
        if verdict["credential_accepted"] and repository is not None:
            # This is what unblocks NEW ENTRIES in the engine's TokenGuard.
            from app.live.guards import TokenGuard

            state = TokenGuard(
                repository,
                max_age_hours=settings.live_token_max_age_hours,
                warn_minutes=settings.live_token_warn_minutes,
            ).record_verified(source="live-preflight", credential=token)
            payload["verification_recorded"] = {
                "verified_at": state.get("verified_at"),
                "source": state.get("source"),
                "credential_bound": bool(state.get("credential_sha256")),
            }
    except SandboxAuthError:
        payload.update(credential_checked=True, credential_accepted=False,
                       probe="credential rejected (401/403)")
    except SandboxRequestError as exc:
        payload.update(credential_checked=True, credential_accepted=False,
                       probe=str(exc))
    print(json.dumps(payload, indent=2, default=str))
    if database is not None:
        database.close()
    return 0 if payload.get("credential_accepted") else 1


def cmd_sandbox_test(args) -> int:
    """Verify SANDBOX credentials against the documented order APIs (read-only).

    Uses the documented GET /order/history with a probe order id:
    401/403 → credential rejected (fail closed); HTTP 400/404 with the
    documented 'order not found' code → credential ACCEPTED (no order placed);
    network error → unreachable. --place-order explicitly submits one real
    sandbox order at a tiny quantity (requires the flag, never default).
    """
    import os

    # Settings MUST be loaded before the token is read: Settings.from_env()
    # is what calls load_dotenv(), so reading os.environ first silently misses
    # a token supplied only via .env. Read the token from Settings rather than
    # the environment so there is exactly one credential path.
    try:
        settings = _load_settings()
    except ValueError as exc:
        print(json.dumps({
            "scope": "REAL UPSTOX SANDBOX TEST",
            "performed": False,
            "reason": f"configuration rejected before any request: {exc}",
        }, indent=2))
        return 2

    token = settings.upstox_sandbox_access_token or os.getenv(
        "UPSTOX_SANDBOX_ACCESS_TOKEN", "")
    if not token.strip():
        print(json.dumps({
            "scope": "REAL UPSTOX SANDBOX TEST",
            "performed": False,
            "reason": "UPSTOX_SANDBOX_ACCESS_TOKEN not set; nothing attempted, nothing claimed",
        }, indent=2))
        return 3
    from app.brokers.sandbox import SandboxAuthError, SandboxRequestError, UpstoxSandboxBroker

    broker = UpstoxSandboxBroker(
        token,
        args.instrument,
        base_url=settings.upstox_sandbox_base_url,
        history_url=settings.upstox_sandbox_order_history_url,
        product=settings.sandbox_product,
        tag=settings.sandbox_order_tag,
    )
    summary = {
        "scope": "REAL UPSTOX SANDBOX TEST",
        "performed": True,
        "credential_accepted": False,
        "order_placed": False,
        "note": "read-only history probe; sandbox orders only; never live",
    }
    try:

        response = broker._request(
            "GET", settings.upstox_sandbox_order_history_url, params={"order_id": "0"}
        )
        from app.brokers.sandbox import classify_history_probe

        verdict = classify_history_probe(response)
        summary["credential_accepted"] = verdict["credential_accepted"]
        summary["probe"] = (f"HTTP {verdict['http_status']} {verdict['class']} "
                            f"codes={verdict.get('codes', [])}")
        if verdict["class"] == "unexpected":
            summary["unexpected_probe_response"] = True
    except SandboxAuthError:
        summary["credential_accepted"] = False
        summary["probe"] = "credential rejected (401/403)"
    except SandboxRequestError as exc:
        summary["probe"] = str(exc)
    if summary["credential_accepted"] and args.place_order:
        # FINAL-11/10 §60: this CLI path may only ever touch the SANDBOX. A
        # live order must never be placed from a CLI flag — real-money
        # submission exists only through the live execution gateway. Refuse
        # loudly rather than submit if the broker is not provably sandbox.
        if getattr(broker, "LIVE", False) or \
                getattr(broker, "MODE", "") != "UPSTOX_SANDBOX":
            summary["order_placed"] = False
            summary["refused"] = (
                "CLI --place-order is sandbox-only; a live broker was "
                "detected, so no order was submitted. Live orders exist "
                "only through the live execution gateway.")
            print(json.dumps(summary, indent=2, default=str))
            return 2
        intent = broker.submit("BUY", args.quantity, 0, order_type="MARKET",
                               instrument_key=args.instrument)
        fills = broker.reconcile()
        summary["order_placed"] = True
        summary["submitted_intent"] = intent[:12]
        summary["fills_after_reconcile"] = fills
        summary["ledger"] = broker.ledger.snapshot()
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["credential_accepted"] else 1


class DatasetBuilderRepositoryShim:
    """DatasetBuilder bound to repository+store without keeping a DB open."""

    def __init__(self, repository, store):
        from app.research.datasets import DatasetBuilder

        self._builder = DatasetBuilder(repository, store)

    def build(self, *args, **kwargs):
        return self._builder.build(*args, **kwargs)

    def audit(self, *args, **kwargs):
        return self._builder.audit(*args, **kwargs)


def _data_stack(settings):
    """Shared V2 data-platform stack (database + repository + store + manager)."""
    from app.database.database import Database
    from app.database.repository import Repository
    from app.history.manager import HistoricalDataManager
    from app.history.store import CandleStore

    database = Database(settings.database_url)
    repository = Repository(database)
    store = CandleStore(Path(settings.data_dir) / "history",
                        parquet_enabled=settings.parquet_enabled)
    manager = HistoricalDataManager(repository, store, settings)
    return database, repository, store, manager


def cmd_initialize(args) -> int:
    """Idempotent first-run initializer (dirs, DB, config, instrument data)."""
    from app.bootstrap import initialize

    result = initialize(_load_settings())
    return 0 if result.get("ok") else 1


def cmd_market_universe(args) -> int:
    settings = _load_settings()
    from app.database.database import Database
    from app.database.repository import Repository
    from app.universe.manager import MarketUniverseManager

    database = Database(settings.database_url)
    try:
        repository = Repository(database)
        manager = MarketUniverseManager(repository, url=settings.universe_sync_url)
        if args.action == "sync":
            result = manager.sync()
        elif args.action == "status":
            result = manager.status()
        elif args.action == "audit":
            result = manager.audit()
        else:
            result = {"ok": False, "error": f"unknown action {args.action}"}
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status", "complete") not in {"unavailable"} or args.action != "sync" else 1
    finally:
        database.close()


def cmd_history(args) -> int:
    settings = _load_settings()
    database, repository, store, manager = _data_stack(settings)
    try:
        if args.action == "sync":
            if not args.instrument:
                print(json.dumps({"ok": False,
                                  "error": "--instrument KEY required (e.g. NSE_EQ|INE002A01018)"}))
                return 2
            unit, interval = (args.interval.split(":") if ":" in args.interval
                              else (args.interval, 1))
            result = manager.sync(args.instrument, unit, int(interval),
                                  args.from_date, args.to_date)
        elif args.action == "status":
            result = manager.status(instrument_key=args.instrument)
        elif args.action == "audit":
            if not args.instrument:
                print(json.dumps({"ok": False, "error": "--instrument KEY required"}))
                return 2
            unit, interval = (args.interval.split(":") if ":" in args.interval
                              else (args.interval, 1))
            result = manager.audit(args.instrument, unit, int(interval))
        else:
            result = {"ok": False, "error": f"unknown action {args.action}"}
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status", "complete") in {"complete", None} or args.action != "sync" else 1
    finally:
        database.close()


def cmd_dataset(args) -> int:
    settings = _load_settings()
    database, repository, store, _manager = _data_stack(settings)
    try:
        builder = DatasetBuilderRepositoryShim(repository, store)
        if args.action == "build":
            keys = [k.strip() for k in args.instruments.split(",") if k.strip()]
            result = builder.build(args.name, keys, args.unit, args.interval,
                                   args.from_date, args.to_date)
        elif args.action == "audit":
            # BUGFIX: without --dataset-id this passed None into the builder,
            # which raised ValueError("unknown dataset: None") as a raw
            # traceback. An omitted argument is a usage error, not a crash.
            if not args.dataset_id:
                print(json.dumps({
                    "ok": False,
                    "error": "'dataset audit' requires --dataset-id <id>",
                    "hint": "list recorded datasets with 'python -m app dataset build' "
                            "output, or inspect them via the research API",
                }, indent=2))
                return 2
            result = builder.audit(args.dataset_id)
        else:
            result = {"ok": False, "error": f"unknown action {args.action}"}
        print(json.dumps(result, indent=2, default=str))
        return 0 if "error" not in result else 1
    finally:
        database.close()


def cmd_universe_screen(args) -> int:
    settings = _load_settings()
    database, repository, store, _manager = _data_stack(settings)
    try:
        from app.universe.selector import UniverseSelector

        selector = UniverseSelector(repository, store)
        result = selector.screen(
            exchange=args.exchange or None, segment=args.segment,
            instrument_types=tuple(t for t in args.types.split(",") if t),
            min_history_records=args.min_history,
            min_price=args.min_price, max_price=args.max_price,
            min_avg_volume=args.min_volume, unit=args.unit,
            interval=args.interval)
        result["eligible"] = result["eligible"][: int(args.limit)]
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        database.close()


def cmd_research(args) -> int:
    settings = _load_settings()
    # Validate arguments BEFORE touching the database or the candle store: a
    # typo in --periods should be reported immediately, not after the tool has
    # loaded history and then discovered it has nothing to do.
    periods: list[int] = []
    if args.action == "run":
        try:
            periods = [int(p.strip()) for p in str(args.periods).split(",")
                       if p.strip()]
        except ValueError:
            print(json.dumps({
                "ok": False,
                "error": f"--periods must be comma-separated integers, got "
                         f"{args.periods!r}",
                "hint": "e.g. --periods 2,3,5",
            }, indent=2))
            return 2
        if not periods:
            print(json.dumps({"ok": False,
                              "error": "--periods must list at least one period"},
                             indent=2))
            return 2
    database, repository, store, _manager = _data_stack(settings)
    try:
        if args.action == "run":
            from app.learning.experiments import ExperimentEngine

            keys = [k.strip() for k in args.instruments.split(",") if k.strip()]
            rows = []
            for key in keys:
                rows.extend(store.read_range(key, args.unit, args.interval,
                                             args.from_date, args.to_date))
            if not rows:
                print(json.dumps({"ok": False,
                                  "error": "no local candles for that request; run `history sync` first"},
                                 indent=2))
                return 2
            from app.backtesting.costs import ExecutionConfig
            # BUGFIX: BacktestEngine was used below but never imported here, so
            # `python -m app research run` died with NameError as soon as it had
            # candles to work with (other call sites import it; this one did not).
            from app.backtesting.engine import BacktestEngine

            from app.data.models import Candle as CandleModel
            from app.history.store import parse_ts

            def symbol_for(key: str) -> str:
                from app.data.models import validate_symbol

                try:
                    found = repository.list_instruments(state=None, limit=10000)
                except Exception:
                    found = []
                match = next((r for r in found if r["instrument_key"] == key), None)
                candidate = (match or {}).get("trading_symbol") or key.split("|")[-1]
                try:
                    return validate_symbol(candidate)
                except ValueError:
                    return "RESEARCH"

            candles = []
            for key in keys:
                rows = store.read_range(key, args.unit, args.interval,
                                        args.from_date, args.to_date)
                candles.extend(CandleModel(parse_ts(r["ts"]), r["open"], r["high"],
                                           r["low"], r["close"], r["volume"],
                                           symbol_for(key)) for r in rows)
            # BUGFIX: --periods is a comma-separated STRING ("2,3,5"), but the
            # grid iterated it directly, yielding the characters '2', ',', '3'
            # — so every parameter set carried period="," and strategy
            # construction raised "period must be an integer between 1 and 2000".
            # This made `research run` unusable. Parsed and validated above.
            grid = [{"period": period, "threshold": 0.0} for period in periods]
            engine = ExperimentEngine(repository,
                                      engine=BacktestEngine(ExecutionConfig()))
            result = engine.run(args.name, candles, "momentum", grid, args.timeframe)
        elif args.action == "status":
            result = {"experiments": repository.list_experiments(limit=10),
                      "promoted_candidates": repository.list_candidates(
                          promoted=True, limit=10)}
        else:
            result = {"ok": False, "error": f"unknown action {args.action}"}
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        database.close()


def cmd_report(args) -> int:
    settings = _load_settings()
    from app.database.database import Database
    from app.database.repository import Repository
    from app.reporting.weekly import WeeklyReport

    database = Database(settings.database_url)
    try:
        repository = Repository(database)
        report = WeeklyReport(repository, settings=settings)
        if args.format == "pdf":
            payload = report.pdf()
            target = Path(args.output or f"arena-weekly-{utc_today_iso()}.pdf")
            target.write_bytes(payload)
            print(json.dumps({"ok": True, "path": str(target), "bytes": len(payload)}))
        else:
            print(report.markdown())
        return 0
    finally:
        database.close()


def cmd_maintenance(args) -> int:
    settings = _load_settings()
    database, repository, store, manager = _data_stack(settings)
    try:
        from datetime import timedelta

        from app.maintenance.scheduler import MaintenanceScheduler
        from app.research.datasets import DatasetBuilder
        from app.universe.manager import MarketUniverseManager

        universe = MarketUniverseManager(repository, url=settings.universe_sync_url)
        datasets = DatasetBuilder(repository, store)

        def coverage_job():
            refreshed = []
            for row in repository.list_coverage(limit=200):
                refreshed.append(manager.refresh_coverage(
                    row["instrument_key"], row["unit"], int(row["interval"])))
            return {"refreshed": len(refreshed)}

        latest = repository.list_dataset_versions(limit=1)
        def dataset_job():
            if not latest:
                return {"skipped": "no datasets defined"}
            record = latest[0]
            keys = json.loads(record["instrument_keys"])
            return datasets.build(record["name"], [k.split("::")[0] for k in keys],
                                  record["unit"], int(record["interval"]))
        from app.maintenance.final_jobs import build_maintenance_jobs

        scheduler = MaintenanceScheduler([
            {"name": "universe_sync", "fn": universe.sync, "interval": timedelta(days=1)},
            {"name": "coverage_refresh", "fn": coverage_job, "interval": timedelta(days=1)},
            {"name": "dataset_refresh", "fn": dataset_job, "interval": timedelta(days=7)},
            *build_maintenance_jobs(
                repository, settings, settings.database_url,
                backup_dir=str(Path(settings.data_dir) / "backups")),
        ])
        result = scheduler.run_due()
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        database.close()


def _stack_or_none(settings):
    from app.database.database import Database
    from app.database.repository import Repository

    database = Database(settings.database_url)
    return database, Repository(database)


def cmd_ask(args) -> int:
    """Command Center: answer a natural-language question from stored data."""
    settings = _load_settings()
    database, repository = _stack_or_none(settings)
    try:
        from app.research.command_center import CommandCenter

        result = CommandCenter(repository).ask(args.question)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        database.close()


def cmd_scan(args) -> int:
    """OpportunityScanner over the eligible universe (research only)."""
    settings = _load_settings()
    database, repository = _stack_or_none(settings)
    try:
        from app.history.store import CandleStore
        from app.learning.regime_engine import RegimeEngine
        from app.research.scanner import OpportunityScanner
        from app.strategies import create_strategy

        store = CandleStore(Path(settings.data_dir) / "history",
                            parquet_enabled=settings.parquet_enabled)
        keys = [k.strip() for k in args.instruments.split(",") if k.strip()]
        strategies = [create_strategy("momentum", {"period": 2, "threshold": 0.0}),
                      create_strategy("rsi", {"period": 14})]
        candidates = repository.list_candidates(promoted=True, limit=20)
        evidence = {}
        for candidate in candidates:
            oos = candidate["oos"] if isinstance(candidate["oos"], dict) else json.loads(candidate["oos"] or "{}")
            evidence[candidate["strategy"]] = oos
        scanner = OpportunityScanner(repository, store, settings=settings,
                                     regime_engine=RegimeEngine(repository))
        opportunities = scanner.scan(keys, strategies,
                                     unit=args.unit, interval=args.interval,
                                     evidence=evidence)
        print(json.dumps({"opportunities": opportunities,
                          "note": "research output only; trading still requires the hard risk gate"},
                         indent=2, default=str))
        return 0
    finally:
        database.close()


def cmd_discover(args) -> int:
    settings = _load_settings()
    from app.data.models import Candle as CandleModel
    from app.history.store import CandleStore, parse_ts
    from app.research.discovery import StrategyDiscoveryEngine

    database, repository = _stack_or_none(settings)
    try:
        store = CandleStore(Path(settings.data_dir) / "history",
                            parquet_enabled=settings.parquet_enabled)
        candles = []
        for key in [k.strip() for k in args.instruments.split(",") if k.strip()]:
            for row in store.read_range(key, args.unit, args.interval,
                                        args.from_date, args.to_date):
                candles.append(CandleModel(parse_ts(row["ts"]), row["open"],
                                           row["high"], row["low"], row["close"],
                                           row["volume"], key.split("|")[-1]))
        if len(candles) < 60:
            print(json.dumps({"ok": False,
                              "error": f"only {len(candles)} local candles; run history sync first"}))
            return 2
        # BUGFIX: this call was written against an older discovery API.
        # StrategyDiscoveryEngine takes (repository, *, settings=...) — it has
        # no `engine` parameter — and discover(name, candles, timeframe)
        # generates its own deterministic spec list internally, so the separate
        # generate_specs()/specs= path and the `qualified` key never existed.
        # The command therefore died with TypeError before evaluating anything.
        discovery = StrategyDiscoveryEngine(repository, settings=settings)
        run = discovery.discover(args.name, candles, args.timeframe)
        results = run.get("results", [])
        qualified = [r for r in results if r.get("tradable")]
        print(json.dumps({
            "experiment_id": run.get("experiment_id"),
            "candidates": len(results),
            "qualified": len(qualified),
            "qualified_detail": [
                {k: q.get(k) for k in ("strategy", "parameters", "oos")}
                for q in qualified],
        }, indent=2, default=str))
        return 0
    finally:
        database.close()


def cmd_lifecycle(args) -> int:
    settings = _load_settings()
    database, repository = _stack_or_none(settings)
    try:
        from app.learning.lifecycle import StrategyLifecycleManager

        manager = StrategyLifecycleManager(repository)
        if args.action == "status":
            rows = []
            for lineage in repository.list_lineage():
                current = repository.current_lifecycle(lineage["id"])
                rows.append({"lineage_id": lineage["id"], "strategy": lineage["strategy"],
                             "generation": lineage["generation"],
                             "stage": (current or {}).get("stage"),
                             "status": (current or {}).get("status"),
                             "tradable": manager.is_tradable(lineage["id"])})
            print(json.dumps({"strategies": rows}, indent=2))
        else:
            result = (manager.promote(args.lineage, evidence={"source": "cli"})
                      if args.action == "promote"
                      else manager.degrade(args.lineage, evidence={"source": "cli"}))
            print(json.dumps(result, indent=2))
        return 0
    finally:
        database.close()


def cmd_replay(args) -> int:
    settings = _load_settings()
    database, repository = _stack_or_none(settings)
    try:
        from app.research.replay import DecisionReplay

        print(json.dumps(DecisionReplay(repository).replay_signal(args.signal),
                         indent=2, default=str))
        return 0
    finally:
        database.close()


def cmd_monitor(args) -> int:
    settings = _load_settings()
    database, repository = _stack_or_none(settings)
    try:
        from app.monitoring.system import SystemMonitor

        snapshot = SystemMonitor(repository, data_dir=settings.data_dir,
                                 database_path=database.path).snapshot()
        print(json.dumps(snapshot, indent=2))
        return 0 if not snapshot.get("alerts") else 1
    finally:
        database.close()


def cmd_backup(args) -> int:
    settings = _load_settings()
    database, repository = _stack_or_none(settings)
    try:
        # verify/restore act on a named file; without one the path is None and
        # Path(None) raises deep inside pathlib. Refuse in plain language here.
        if args.action in ("verify", "restore") and not args.path:
            print(json.dumps({
                "ok": False,
                "error": f"'backup {args.action}' requires --path <backup file>",
                "hint": f"backups live in {args.backup_dir}/ "
                        f"(default: the DATA_DIR backups directory)",
            }, indent=2))
            return 1
        from app.database.recovery import RecoveryManager

        recovery = RecoveryManager(database, repository,
                                   backup_dir=args.backup_dir)
        if args.action == "backup":
            result = recovery.backup(args.label)
        elif args.action == "verify":
            result = recovery.verify(args.path)
        else:
            result = recovery.restore(args.path)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok", result.get("restored", True)) else 1
    finally:
        database.close()


def cmd_upstox_login(args) -> int:
    """Official OAuth 2.0 authorization-code flow; token goes to .env only."""
    from app.data.upstox_auth import run_login_flow

    # NOT ROOT/.env: when packaged, ROOT is PyInstaller's temporary extraction
    # directory, so a refreshed token would be written to a file that is
    # deleted on exit. The operator would see "login complete" and the engine
    # would still hold the stale credential.
    from app import runtime

    run_login_flow(runtime.env_file())
    return 0


def parser() -> argparse.ArgumentParser:
    from app import __version__

    p = argparse.ArgumentParser(
        prog="python -m app",
        description=f"Arena {__version__} — trading research platform. "
                    "PAPER by default; LIVE=real money is gated behind the "
                    "four-switch opt-in (see LIVE_TRADING_ENABLEMENT.md).")
    p.add_argument("--version", action="version",
                   version=f"Arena {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    test = sub.add_parser("upstox-test", help="test Upstox auth + connectivity (read-only)")
    test.add_argument("--timeout", type=float, default=15.0,
                      help="seconds to wait for a first completed candle")
    test.set_defaults(func=cmd_upstox_test)

    stream = sub.add_parser("market-stream", help="print normalized real market events")
    stream.add_argument("--duration", type=float, default=60.0,
                        help="seconds to stream before stopping")
    stream.set_defaults(func=cmd_market_stream)

    live = sub.add_parser("paper-live", help="full Upstox → candles → strategies → risk → paper pipeline")
    live.add_argument("passthrough", nargs="*", help=argparse.SUPPRESS)
    live.set_defaults(func=cmd_paper_live)

    test = sub.add_parser("paper-test", help="offline deterministic paper-pipeline self-test (zero dependencies)")
    test.add_argument("--database", help="optional SQLite path (default: isolated temp file)")
    test.set_defaults(func=cmd_paper_test)

    login = sub.add_parser("upstox-login", help="OAuth login helper; stores token in gitignored .env")
    login.set_defaults(func=cmd_upstox_login)

    engine = sub.add_parser(
        "engine",
        help="24/7 engine (requires BROKER=UPSTOX_SANDBOX or UPSTOX_LIVE + DATA_SOURCE=upstox)")
    engine.add_argument("--max-candles", type=int, default=None,
                        help="bound the run for tests/verification (default: unlimited)")
    engine.set_defaults(func=cmd_engine)

    pre = sub.add_parser(
        "live-preflight",
        help="LIVE readiness check (read-only; never places an order)")
    pre.add_argument("--instrument", default="NSE_EQ|INE002A01018")
    pre.set_defaults(func=cmd_live_preflight)

    halt = sub.add_parser(
        "live-halt",
        help="set the persisted EMERGENCY STOP (always permitted)")
    halt.add_argument("--reason", default="")
    halt.set_defaults(func=cmd_live_halt)

    resume = sub.add_parser(
        "live-resume",
        help="clear the persisted EMERGENCY STOP (requires an armed live session)")
    resume.add_argument("--allow-non-live", action="store_true",
                        help="also clear when the live opt-in is NOT configured")
    resume.set_defaults(func=cmd_live_resume)

    lstatus = sub.add_parser(
        "live-status",
        help="print the live-ops snapshot from recorded state (read-only)")
    lstatus.add_argument("--audit", action="store_true",
                         help="also recompute PnL from the audit trail")
    lstatus.set_defaults(func=cmd_live_status)

    digest = sub.add_parser(
        "live-digest",
        help="read-only roll-up of alerts, rails, orders and audit-trail PnL")
    digest.add_argument("--hours", type=float, default=24.0,
                        help="window in hours (default 24)")
    digest.add_argument("--post", action="store_true",
                        help="also POST it to ALERT_WEBHOOK_URL if configured")
    digest.add_argument("--markdown", action="store_true",
                        help="print human-readable markdown instead of JSON")
    digest.add_argument("--out", default="",
                        help="also write the report to this path")
    digest.set_defaults(func=cmd_live_digest)

    ack = sub.add_parser(
        "live-ack",
        help="acknowledge alerts (local bookkeeping only; no orders, no limits)")
    ack.add_argument("--id", action="append", default=[],
                     help="alert id to acknowledge (repeatable)")
    ack.add_argument("--kind", default="",
                     help="acknowledge every outstanding alert of this kind")
    ack.add_argument("--all", action="store_true",
                     help="acknowledge every recorded alert")
    ack.add_argument("--by", default="", help="who is acknowledging")
    ack.add_argument("--note", default="", help="free-text note")
    ack.set_defaults(func=cmd_live_ack)

    drill = sub.add_parser(
        "live-drill",
        help="offline fire drill for the safety rails (no network, no orders)")
    drill.add_argument("--database-url", default="",
                       help="scratch database to drill against (default: a "
                            "throwaway temporary one)")
    drill.set_defaults(func=cmd_live_drill)

    stest = sub.add_parser("sandbox-test", help="verify SANDBOX credentials (read-only probe)")
    stest.add_argument("--instrument", default="NSE_EQ|INE002A01018")
    stest.add_argument("--place-order", action="store_true",
                       help="EXPLICITLY place one small sandbox order after verification")
    stest.add_argument("--quantity", type=int, default=1)
    stest.set_defaults(func=cmd_sandbox_test)

    init = sub.add_parser("initialize", help="idempotent first-run setup (dirs, DB, instruments)")
    init.set_defaults(func=cmd_initialize)

    uni = sub.add_parser("market-universe", help="instrument master: sync/status/audit")
    uni.add_argument("action", choices=["sync", "status", "audit"])
    uni.set_defaults(func=cmd_market_universe)

    hist = sub.add_parser("history", help="historical candles: sync/status/audit")
    hist.add_argument("action", choices=["sync", "status", "audit"])
    hist.add_argument("--instrument", default=None)
    hist.add_argument("--interval", default="days:1", help="unit:interval e.g. minutes:1, days:1")
    hist.add_argument("--from-date", default="2025-01-01")
    hist.add_argument("--to-date", default=utc_today_iso())
    hist.set_defaults(func=cmd_history)

    dset = sub.add_parser("dataset", help="versioned research datasets: build/audit")
    dset.add_argument("action", choices=["build", "audit"])
    dset.add_argument("--name", default="research")
    dset.add_argument("--instruments", default="NSE_EQ|INE002A01018")
    dset.add_argument("--unit", default="days")
    dset.add_argument("--interval", type=int, default=1)
    dset.add_argument("--from-date", default=None)
    dset.add_argument("--to-date", default=None)
    dset.add_argument("--dataset-id", default=None)
    dset.set_defaults(func=cmd_dataset)

    screen = sub.add_parser("universe", help="screen the eligible research universe")
    screen.add_argument("action", choices=["screen"])
    screen.add_argument("--exchange", default="NSE")
    screen.add_argument("--segment", default=None)
    screen.add_argument("--types", default="EQ")
    screen.add_argument("--min-history", type=int, default=0)
    screen.add_argument("--min-price", type=float, default=None)
    screen.add_argument("--max-price", type=float, default=None)
    screen.add_argument("--min-volume", type=float, default=None)
    screen.add_argument("--unit", default="days")
    screen.add_argument("--interval", type=int, default=1)
    screen.add_argument("--limit", type=int, default=25)
    screen.set_defaults(func=cmd_universe_screen)

    research = sub.add_parser("research", help="parameter experiments: run/status")
    research.add_argument("action", choices=["run", "status"])
    research.add_argument("--name", default="grid")
    research.add_argument("--instruments", default="NSE_EQ|INE002A01018")
    research.add_argument("--unit", default="days")
    research.add_argument("--interval", type=int, default=1)
    research.add_argument("--from-date", default=None)
    research.add_argument("--to-date", default=None)
    research.add_argument("--timeframe", default="1D")
    research.add_argument("--periods", default="2,3,5")
    research.set_defaults(func=cmd_research)

    report = sub.add_parser("report", help="human-readable weekly report")
    report.add_argument("--format", choices=["markdown", "pdf"], default="markdown")
    report.add_argument("--output", default=None)
    report.set_defaults(func=cmd_report)

    maint = sub.add_parser("maintenance", help="run due maintenance jobs once")
    maint.set_defaults(func=cmd_maintenance)

    ask = sub.add_parser("ask", help="Command Center: ask about recorded data")
    ask.add_argument("question")
    ask.set_defaults(func=cmd_ask)

    scan = sub.add_parser("scan", help="scan the eligible universe for opportunities")
    scan.add_argument("--instruments", required=True)
    scan.add_argument("--unit", default="days")
    scan.add_argument("--interval", type=int, default=1)
    scan.set_defaults(func=cmd_scan)

    discover = sub.add_parser("discover", help="controlled strategy discovery run")
    discover.add_argument("--name", default="discovery")
    discover.add_argument("--instruments", required=True)
    discover.add_argument("--unit", default="days")
    discover.add_argument("--interval", type=int, default=1)
    discover.add_argument("--from-date", default=None)
    discover.add_argument("--to-date", default=None)
    discover.add_argument("--timeframe", default="1D")
    discover.set_defaults(func=cmd_discover)

    life = sub.add_parser("lifecycle", help="strategy pipeline: status/promote/degrade")
    life.add_argument("action", choices=["status", "promote", "degrade"])
    life.add_argument("--lineage", default=None)
    life.set_defaults(func=cmd_lifecycle)

    replay = sub.add_parser("replay", help="reconstruct a decision from recorded rows")
    replay.add_argument("--signal", required=True)
    replay.set_defaults(func=cmd_replay)

    mon = sub.add_parser("monitor", help="self-monitoring snapshot (exit 1 on alerts)")
    mon.set_defaults(func=cmd_monitor)

    bak = sub.add_parser("backup", help="database backup/verify/restore")
    bak.add_argument("action", choices=["backup", "verify", "restore"])
    bak.add_argument("--path", default=None)
    bak.add_argument("--label", default="manual")
    bak.add_argument("--backup-dir", default="backups")
    bak.set_defaults(func=cmd_backup)
    return p


def _operator_errors() -> tuple[type[BaseException], ...]:
    """Exception types that mean "the operator must change something".

    These carry actionable messages (missing credentials, a refused host, a
    rejected sandbox call) and were previously reaching the user as a ~12-line
    traceback ending in ``raise UpstoxOAuthError(...)``. Anything NOT in this
    tuple keeps its traceback, because an unexpected exception is a bug and the
    stack is how it gets diagnosed.
    """
    types: list[type[BaseException]] = []
    for module_name, class_name in (
        ("app.data.upstox_auth", "UpstoxOAuthError"),
        ("app.data.upstox_stream", "UpstoxAuthError"),
        ("app.brokers.sandbox", "SandboxAuthError"),
        ("app.brokers.sandbox", "SandboxRequestError"),
    ):
        try:
            module = importlib.import_module(module_name)
            candidate = getattr(module, class_name, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                types.append(candidate)
        except Exception:  # pragma: no cover - optional/missing dependency
            continue
    return tuple(types)


def main(argv=None) -> None:
    # Windows consoles are not UTF-8 by default; without this the help text
    # kills the process with UnicodeEncodeError before printing anything.
    from app.runtime import ensure_utf8_output

    ensure_utf8_output()
    args = parser().parse_args(argv)
    try:
        raise SystemExit(args.func(args))
    except SystemExit:
        raise
    except _operator_errors() as exc:
        print(json.dumps({"ok": False, "error": str(exc),
                          "error_type": type(exc).__name__}, indent=2), flush=True)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
