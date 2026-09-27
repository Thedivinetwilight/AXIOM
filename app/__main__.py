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

There is no command that can place a real order. MODE=PAPER and
LIVE_TRADING=false are hard-enforced by Settings for every command.
"""

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
import sys


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
    """Full paper pipeline over the real data source (existing run.py engine)."""
    from run import main as run_main

    passthrough = argv[args.passthrough:] if args.passthrough else []
    run_main(["--mode", "paper", "--source", "upstox", *passthrough])
    return 0


def cmd_engine(args) -> int:
    """24/7 engine: Upstox V3 data → strategies → hard risk → sandbox orders.

    Requires explicit configuration: BROKER=UPSTOX_SANDBOX (plus the sandbox
    token and DATA_SOURCE=upstox). LOCAL_PAPER users keep the existing paper
    pipeline (`paper-live`/run.py), which this command refuses to replace.
    """
    settings = _load_settings()
    if settings.broker.upper() != "UPSTOX_SANDBOX":
        print(json.dumps({
            "ok": False,
            "error": "engine requires explicit BROKER=UPSTOX_SANDBOX configuration; "
                     "LOCAL_PAPER continues via 'python -m app paper-live'",
        }, indent=2))
        return 2
    if (settings.data_source != "upstox"):
        print(json.dumps({"ok": False,
                          "error": "engine requires DATA_SOURCE=upstox (real market data)"},
                         indent=2))
        return 2

    from app.backtesting.costs import ExecutionConfig
    from app.brokers.sandbox import UpstoxSandboxBroker
    from app.data.factory import stream_provider
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
    broker = UpstoxSandboxBroker(
        settings.upstox_sandbox_access_token,
        provider._keys[0],
        base_url=settings.upstox_sandbox_base_url,
        history_url=settings.upstox_sandbox_order_history_url,
        product=settings.sandbox_product,
        tag=settings.sandbox_order_tag,
        repository=repository,
        initial_capital=ExecutionConfig(**settings.execution_kwargs()).initial_capital,
    )
    from app.engine.adaptive import RouterStrategySource
    from app.engine.events import EventDrivenDecisionEngine, PauseGate
    from app.learning.calibration import ConfidenceCalibration
    from app.maintenance.final_jobs import build_maintenance_jobs
    from app.maintenance.scheduler import MaintenanceScheduler

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
        broker=broker,
        repository=repository,
        risk_limits=HardRiskLimits.from_settings(settings),
        journal=LearningJournal(repository),
        timeframe=settings.default_timeframe if settings.default_timeframe in {"1m", "5m", "15m", "30m"} else "1m",
        event_engine=event_engine,
        pause_gate=pause_gate,
        maintenance_scheduler=scheduler,
        strategy_source=strategy_source,
        instrument_keys=instrument_keys,
        market_timezone=settings.market_timezone,
        stop_loss=float(settings.stop_loss or 0.0),
        take_profit=float(settings.take_profit or 0.0),
    )
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


def cmd_sandbox_test(args) -> int:
    """Verify SANDBOX credentials against the documented order APIs (read-only).

    Uses the documented GET /order/history with a probe order id:
    401/403 → credential rejected (fail closed); HTTP 400/404 with the
    documented 'order not found' code → credential ACCEPTED (no order placed);
    network error → unreachable. --place-order explicitly submits one real
    sandbox order at a tiny quantity (requires the flag, never default).
    """
    import os

    token = os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
    if not token.strip():
        print(json.dumps({
            "scope": "REAL UPSTOX SANDBOX TEST",
            "performed": False,
            "reason": "UPSTOX_SANDBOX_ACCESS_TOKEN not set; nothing attempted, nothing claimed",
        }, indent=2))
        return 3
    from app.brokers.sandbox import SandboxAuthError, SandboxRequestError, UpstoxSandboxBroker

    settings = _load_settings()
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
        import httpx as _httpx

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
            grid = [{"period": p, "threshold": 0.0} for p in args.periods]
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
            target = Path(args.output or f"arena-weekly-{date.today().isoformat()}.pdf")
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
    from app.backtesting.costs import ExecutionConfig
    from app.backtesting.engine import BacktestEngine
    from app.data.models import Candle as CandleModel
    from app.history.store import CandleStore, parse_ts
    from app.research.discovery import StrategyDiscoveryEngine, generate_specs

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
        discovery = StrategyDiscoveryEngine(
            repository, engine=BacktestEngine(ExecutionConfig()))
        specs = generate_specs(max_candidates=settings.discovery_max_candidates)
        run = discovery.discover(args.name, candles, args.timeframe, specs=specs)
        print(json.dumps({"experiment_id": run["experiment_id"],
                          "candidates": len(run["results"]),
                          "qualified": len(run["qualified"]),
                          "qualified_detail": [{k: q.get(k) for k in ("strategy", "parameters", "score", "oos")}
                                               for q in run["qualified"]]},
                         indent=2, default=str))
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

    from app.config.settings import ROOT

    run_login_flow(ROOT / ".env")
    return 0


def parser() -> argparse.ArgumentParser:
    from app import __version__

    p = argparse.ArgumentParser(
        prog="python -m app",
        description=f"Arena {__version__} — PAPER ONLY trading research "
                    "(real-money order routing is absent)")
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

    engine = sub.add_parser("engine", help="24/7 engine (requires BROKER=UPSTOX_SANDBOX + DATA_SOURCE=upstox)")
    engine.add_argument("--max-candles", type=int, default=None,
                        help="bound the run for tests/verification (default: unlimited)")
    engine.set_defaults(func=cmd_engine)

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
    hist.add_argument("--to-date", default=date.today().isoformat())
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


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
