from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import dataclasses as _dataclasses
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from app.api.auth import authenticate
from app import __version__
from app.config.settings import ROOT, Settings
from app.database.database import Database
from app.database.repository import Repository
from app.backtesting.costs import ExecutionConfig
from app.paper_trading.broker import PaperBroker
from app.paper_trading.execution import make_calendar
from app.paper_trading.simulator import PaperSimulator
from app.api.routes import (
    dashboard, health, market, strategies, backtests, paper, v2, market_events, chart, terminal,
    market_data, listed_assets, connections, alpaca_broker, markets, model_lab, ensemble,
    performance as performance_routes, scan_capacity,
)
# NOTE: ``app.api.routes.vantage`` (the legacy ``/api/trading`` MT5 order API) is
# deliberately NOT imported or mounted. It was never documented in the product
# documentation and its operator UI was removed in v3.7.0a9; the supported
# application is NSE (Upstox) / NASDAQ (Alpaca) paper plus gated live trading.
# ``tests/test_vantage_live_api_removal.py`` fails if it is mounted again.
from app.vantage.models import SessionMode
from app.vantage.connector import BrokerError
from app.vantage.gateway import MT5PackageUnavailable
from app.vantage.risk import ContractViolation
from app.vantage.secrets import DpapiSecretStore, SecretStoreUnavailable
from app.vantage.service import OrderBlocked, ServiceError, VantageService
from app.vantage.store import VantageStore
from app.market_data.mt5 import MarketDataError, MT5MarketDataService
from app.performance import LatencyTracker
from app.listed_assets import ListedAssetService, ListedProviderUnavailable
from app.autonomy.config import AutonomySettings
from app.autonomy.lease import LeaseUnavailable, SingleInstanceLease
from app.autonomy.service import AutonomyService
from app.autonomy.store import AutonomyStore
from app.autonomy.upstox import InstrumentCatalog
from app.autonomy.upstox_token import default_live_store as upstox_live_default_store
from app.autonomy.upstox_token import default_store as upstox_token_default_store
from app.autonomy.upstox_token import (
    resolve_live_token,
    resolve_live_token_provider,
    resolve_token_provider,
)
from app.autonomy.runner import AutonomyRunner
from app.api.routes import autonomy


def create_app(settings: Settings | None = None, data_dir: Path | None = None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        database = Database(settings.database_url)
        repository = Repository(database)
        vantage_store = VantageStore(database)
        try:
            broker = PaperBroker(
                ExecutionConfig(**settings.execution_kwargs()),
                currency=settings.currency,
                timeframe=settings.default_timeframe,
                repository=repository,
                calendar=make_calendar(settings.market_calendar, settings.default_timeframe),
                risk_limits=settings.paper_risk_limits(),
            )
            app.state.repository, app.state.broker = repository, broker
            app.state.simulator = PaperSimulator(broker)
            database_path = settings.database_url[len("sqlite:///"):]
            secret_dir = (
                Path(app.state.data_dir) / "secrets" if database_path == ":memory:"
                else Path(database_path).parent / "secrets"
            )
            try:
                secret_store = DpapiSecretStore(secret_dir)
                app.state.vantage_secret_status = "Windows DPAPI · current Windows user"
            except SecretStoreUnavailable:
                secret_store = None
                app.state.vantage_secret_status = "Unavailable on this host; plaintext fallback is disabled"
            app.state.vantage = VantageService(
                vantage_store, secret_store,
                lock_check=lambda: bool(repository._writer_file or repository._memory_writer),
                metrics=app.state.performance,
            )
            app.state.market_data = MT5MarketDataService(
                secret_store,
                execution_mode=lambda: app.state.vantage.mode,
                audit=repository.record_system_event,
                metrics=app.state.performance,
            )
            is_ephemeral_memory = database_path == ":memory:" and not app.state.explicit_data_dir
            runtime_base = (
                None
                if is_ephemeral_memory
                else (
                    Path(app.state.data_dir)
                    if (app.state.explicit_data_dir or database_path == ":memory:")
                    else Path(database_path).parent
                )
            )
            if runtime_base is not None and runtime_base.resolve() == (ROOT / "data").resolve():
                runtime_base = runtime_base / "autonomy"
            from app.markets.alpaca_credentials import (
                default_alpaca_data_store,
                resolve_alpaca_data_credentials,
            )
            from app.markets.nasdaq_catalog import NasdaqCatalog

            app.state.alpaca_data_store = default_alpaca_data_store(runtime_base)
            app.state.nasdaq_catalog = NasdaqCatalog(
                (runtime_base / "nasdaq-catalog.json") if runtime_base is not None else None
            )

            def _us_live_quote_lookup(sym: str):
                creds, _ = resolve_alpaca_data_credentials(app.state.alpaca_data_store, settings)
                if not creds["api_key"] or not creds["api_secret"]:
                    return None
                from app.data.alpaca import AlpacaQuoteReader

                reader = AlpacaQuoteReader(creds["api_key"], creds["api_secret"], feed=creds["feed"])
                try:
                    return reader.latest_quote(sym)
                finally:
                    reader.close()

            def _us_asset_lookup(sym: str):
                found = app.state.nasdaq_catalog.get(sym)
                if found is not None:
                    return {"listing_venue": found.exchange, "tradable": found.tradable, "fractionable": found.fractionable}
                creds, _ = resolve_alpaca_data_credentials(app.state.alpaca_data_store, settings)
                if creds["api_key"] and creds["api_secret"]:
                    from app.data.alpaca import AlpacaAssetLookup

                    lkp = AlpacaAssetLookup(creds["api_key"], creds["api_secret"])
                    try:
                        return lkp.lookup(sym)
                    finally:
                        lkp.close()
                raise ValueError(f"Asset {sym} not found in NASDAQ catalog")

            # Alpaca broker path (separately gated; disabled by default). LIVE
            # binds its client only after the gate's explicit Arm + Activate.
            from app.alpaca_broker.runtime import AlpacaBrokerRuntime

            app.state.alpaca_broker_runtime = AlpacaBrokerRuntime(
                settings=settings,
                secret_dir=secret_dir if runtime_base is not None else None,
                journal_path=":memory:" if runtime_base is None else (runtime_base / "alpaca_broker.sqlite3"),
                repository=repository,
                quote_provider=_us_live_quote_lookup,
                asset_lookup=_us_asset_lookup,
            )
            app.state.alpaca_broker = app.state.alpaca_broker_runtime.service
            app.state.alpaca_broker_credential_source = app.state.alpaca_broker_runtime.credential_source
            # Manual Upstox access-token entry: protected per-user storage only.
            # Never creates a session, changes execution mode, or starts
            # automation. The stored token feeds market data and read-only
            # account checks; the OAuth/CLI environment token stays available.
            try:
                app.state.upstox_token = upstox_token_default_store(secret_dir)
                app.state.upstox_live_token = upstox_live_default_store(secret_dir)
            except Exception:
                app.state.upstox_token = None
                app.state.upstox_live_token = None
                logging.getLogger("bot.autonomy").error(
                    "upstox_token_store_unavailable", extra={"error": "store_init"}
                )
            # Autonomy is an isolated boundary.  Startup may load public BOD
            # metadata and a halted journal, but never creates/Arms/Activates
            # a session and never starts with an execution permit.
            app.state.autonomy = None
            app.state.autonomy_runner = None
            app.state.autonomy_error = ""
            autonomy_store = None
            autonomy_lease = None
            try:
                autonomy_settings = AutonomySettings.from_env()
                autonomy_base = Path(app.state.data_dir)
                if not app.state.explicit_data_dir and database_path != ":memory:":
                    # Frozen/production settings already point the legacy DB at
                    # the per-user writable directory; keep autonomy state next
                    # to it rather than inside the read-only bundle.
                    autonomy_base = Path(database_path).parent
                autonomy_dir = autonomy_base / "autonomy"
                autonomy_catalog = InstrumentCatalog(
                    autonomy_settings, autonomy_dir / "instrument-catalog.json"
                )
                autonomy_catalog.load_cache(require_current_day=False)
                autonomy_store = AutonomyStore(autonomy_dir / "journal.sqlite3")
                # Single-instance lease: Arm/Activate require this process to
                # hold it. A second instance keeps running read-only and fails
                # closed at Arm with a named blocker.
                autonomy_lease = SingleInstanceLease(autonomy_dir)
                try:
                    autonomy_lease.acquire()
                except LeaseUnavailable:
                    logging.getLogger("bot.autonomy").warning(
                        "autonomy_lease_held_by_other_instance",
                        extra={"effect": "arm_activate_blocked"},
                    )
                app.state.autonomy_lease = autonomy_lease
                app.state.autonomy = AutonomyService(
                    autonomy_settings, autonomy_store, autonomy_catalog,
                    data_dir=autonomy_dir,
                    token_provider=resolve_token_provider(app.state.upstox_token),
                    live_token_provider=resolve_live_token_provider(app.state.upstox_live_token),
                    instance_lease=autonomy_lease,
                )
                if autonomy_settings.worker_enabled:
                    app.state.autonomy_runner = AutonomyRunner(
                        app.state.autonomy, autonomy_settings.scan_interval_seconds
                    )
                    await app.state.autonomy_runner.start()
            except Exception as exc:
                # Legacy research/paper remains available, while autonomy is
                # explicitly unavailable rather than silently falling back.
                app.state.autonomy_error = f"Autonomy initialization failed: {type(exc).__name__}"
                logging.getLogger("bot.autonomy").error(
                    "autonomy_initialization_failed", extra={"error": type(exc).__name__}
                )
                if autonomy_store is not None:
                    autonomy_store.close()
                if autonomy_lease is not None:
                    autonomy_lease.release()

            from app.markets.live_paper import LiveMarketPaperEngine, MultiMarketPaperRunner
            from app.markets.nse_reconciliation import resolve_nse_instrument_strict
            from app.autonomy.upstox import UpstoxReadOnly
            from app.autonomy.upstox_token import resolve_token

            app.state.nse_seed_catalog = InstrumentCatalog(
                AutonomySettings(), Path(app.state.data_dir) / "nse-seed-fallback.json"
            )
            app.state.nse_seed_catalog.load_seed_fallback()

            def _resolve_nse_instrument(symbol_or_key: str):
                cat = (
                    app.state.autonomy.catalog
                    if (app.state.autonomy is not None and app.state.autonomy.catalog.instruments)
                    else app.state.nse_seed_catalog
                )
                return resolve_nse_instrument_strict(cat, symbol_or_key, allow_index=False)

            def _resolve_nasdaq_instrument(symbol_or_key: str):
                found = app.state.nasdaq_catalog.get(symbol_or_key)
                return found.public() if found is not None else None

            def _nse_live_quote_lookup(instrument_key: str):
                tok, _ = resolve_token(app.state.upstox_token)
                if not tok:
                    tok = str(getattr(settings, "market_data_access_token", "") or "").strip()
                if not tok:
                    raise RuntimeError("Upstox read-only market-data token is not configured")
                snap = UpstoxReadOnly(lambda: tok).quote_snapshot(instrument_key)
                recv_iso = datetime.now(UTC).isoformat()
                return {
                    "instrument_key": snap.instrument_key,
                    "bid": snap.bid,
                    "ask": snap.ask,
                    "last": snap.last,
                    "volume": snap.volume,
                    "timestamp": snap.timestamp.isoformat(),
                    "source_timestamp": snap.timestamp.isoformat(),
                    "local_received_at": recv_iso,
                    "feed": "upstox_nse_v2",
                    "feed_entitlement_label": "Upstox NSE Cash Read-Only Feed (NSE_EQ)",
                    "is_delayed": False,
                    "source": "upstox-market-quote-v2",
                }

            def _nasdaq_live_quote_lookup(symbol: str):
                quote = _us_live_quote_lookup(symbol)
                if quote is None:
                    raise RuntimeError("Alpaca read-only market-data credentials are not configured")
                return quote

            def _nse_live_bars_lookup(symbol_or_key: str, timeframe: str):
                inst = _resolve_nse_instrument(symbol_or_key)
                if inst is None:
                    raise RuntimeError(f"Unknown NSE instrument {symbol_or_key!r}")
                tok, _ = resolve_token(app.state.upstox_token)
                if not tok:
                    tok = str(getattr(settings, "market_data_access_token", "") or "").strip()
                if not tok:
                    raise RuntimeError("Upstox read-only market-data token is not configured")
                tf_fetch = "1D" if timeframe == "1d" else timeframe
                reader = UpstoxReadOnly(lambda: tok)
                bars = reader.historical_candles(inst["instrument_key"], inst["display_symbol"], tf_fetch)
                return bars

            def _nasdaq_live_bars_lookup(symbol_or_key: str, timeframe: str):
                creds, _ = resolve_alpaca_data_credentials(app.state.alpaca_data_store, settings)
                if not creds["api_key"] or not creds["api_secret"]:
                    raise RuntimeError("Alpaca read-only market-data credentials are not configured")
                from app.data.alpaca import AlpacaHistoricalProvider

                tf_fetch = "1h" if timeframe == "4h" else ("1D" if timeframe in {"1d", "1D"} else timeframe)
                lookback_days = 240 if tf_fetch == "1D" else (30 if tf_fetch == "1h" else 7)
                now_utc = datetime.now(UTC)
                prov = AlpacaHistoricalProvider(creds["api_key"], creds["api_secret"], feed=creds["feed"])
                try:
                    # AlpacaHistoricalProvider's public API is MarketDataProvider.historical();
                    # there is no get_candles() on the adapter.
                    return prov.historical(
                        symbol_or_key.strip().upper(),
                        tf_fetch,
                        now_utc - timedelta(days=lookback_days),
                        now_utc,
                    )
                finally:
                    prov.close()

            import tempfile
            from app.ensemble.service import StrategyEnsembleService
            from app.model_lab.kronos_provider import KronosForecastProvider
            from app.model_lab.registry import ModelLabService

            ephemeral_model_lab_tmp = None
            if runtime_base is None:
                ephemeral_model_lab_tmp = tempfile.TemporaryDirectory(
                    prefix="axiom_model_lab_", ignore_cleanup_errors=True
                )
                model_lab_dir = Path(ephemeral_model_lab_tmp.name)
            else:
                model_lab_dir = runtime_base / "model_lab"

            app.state.owner_verification_receipts = {}
            app.state.nse_bars_provider = _nse_live_bars_lookup
            app.state.nasdaq_bars_provider = _nasdaq_live_bars_lookup
            app.state.kronos_provider = KronosForecastProvider(
                app.state.autonomy.kronos if app.state.autonomy is not None else None,
                setup_manager=app.state.autonomy.kronos_setup if app.state.autonomy is not None else None,
                config_path=(model_lab_dir / "kronos-lab-config.json") if runtime_base is not None else None,
            )
            app.state.simulator.kronos_provider = app.state.kronos_provider
            app.state.model_lab = ModelLabService(
                model_lab_dir,
                kronos_provider=app.state.kronos_provider,
                repo_data_dir=ROOT / "data",
            )

            # One bounded, rate-limit-aware provider gate per market, shared by
            # the paper engine and the ensemble service so a cycle's quotes and
            # bar history draw on a single request budget. Bounds come from
            # MARKET_DATA_FETCH_* settings (never from a trading limit).
            from dataclasses import replace as _dc_replace

            from app.markets.fetch_gate import (
                FetchGateConfig,
                ProviderFetchGate,
                alpaca_market_data_policies,
                upstox_standard_policies,
            )

            _base_fetch_cfg = FetchGateConfig(
                max_workers=int(getattr(settings, "market_data_fetch_workers", 8)),
                max_requests_per_second=float(getattr(settings, "market_data_fetch_max_rps", 50.0)),
                burst=max(1, int(getattr(settings, "market_data_fetch_max_rps", 50.0))),
                batch_timeout_seconds=float(
                    getattr(settings, "market_data_fetch_batch_timeout_seconds", 45.0)
                ),
            )
            # The documented windows are enforced per endpoint category. They are
            # not a trading limit and never widen one; they only pace requests.
            _quota_on = str(getattr(settings, "market_data_quota_profile", "documented")).lower() != "off"
            _alpaca_plan = str(getattr(settings, "alpaca_market_data_plan", "basic")).strip().lower()
            _nse_cfg = (
                _dc_replace(
                    _base_fetch_cfg,
                    quota_policies=upstox_standard_policies(),
                    quota_profile="upstox_standard",
                )
                if _quota_on
                else _base_fetch_cfg
            )
            _nasdaq_cfg = (
                _dc_replace(
                    _base_fetch_cfg,
                    quota_policies=alpaca_market_data_policies(_alpaca_plan),
                    quota_profile=f"alpaca_{_alpaca_plan}",
                )
                if _quota_on
                else _base_fetch_cfg
            )
            _fetch_gates = {
                "NSE": ProviderFetchGate(_nse_cfg),
                "NASDAQ": ProviderFetchGate(_nasdaq_cfg),
            }
            app.state.market_data_fetch_gates = _fetch_gates

            app.state.live_paper_engine = LiveMarketPaperEngine(
                ":memory:" if runtime_base is None else (runtime_base / "live_market_paper.sqlite3"),
                nse_quote_provider=_nse_live_quote_lookup,
                nasdaq_quote_provider=_nasdaq_live_quote_lookup,
                nse_bars_provider=_nse_live_bars_lookup,
                nasdaq_bars_provider=_nasdaq_live_bars_lookup,
                nse_fetch_gate=_fetch_gates["NSE"],
                nasdaq_fetch_gate=_fetch_gates["NASDAQ"],
                kronos_provider=app.state.kronos_provider,
                nse_instrument_resolver=_resolve_nse_instrument,
                nasdaq_instrument_resolver=_resolve_nasdaq_instrument,
            )
            app.state.ensemble_service = StrategyEnsembleService(
                ":memory:" if runtime_base is None else (runtime_base / "ensemble.sqlite3"),
                dataset_manager=app.state.model_lab.datasets,
                kronos_provider=app.state.kronos_provider,
                paper_engine=app.state.live_paper_engine,
                nse_quote_provider=_nse_live_quote_lookup,
                nasdaq_quote_provider=_nasdaq_live_quote_lookup,
                nse_bars_provider=_nse_live_bars_lookup,
                nasdaq_bars_provider=_nasdaq_live_bars_lookup,
                nse_instrument_resolver=_resolve_nse_instrument,
                nasdaq_instrument_resolver=_resolve_nasdaq_instrument,
                nse_fetch_gate=_fetch_gates["NSE"],
                nasdaq_fetch_gate=_fetch_gates["NASDAQ"],
            )
            app.state.live_paper_engine.ensemble_service = app.state.ensemble_service

            # --- Quota-aware read-only scan capacity -----------------------
            # Planning only: reads the SAME documented quota policies the gates
            # enforce plus credential PRESENCE (never the value). The single
            # start path calls evaluate_watchlist_cycle with execute_in_paper
            # forced off, so no paper or broker order can be created here.
            from app.markets.scan_capacity import ScanCapacityService

            def _scan_credential_present(market: str) -> bool:
                try:
                    if str(market).upper() == "NSE":
                        token, _source = resolve_token(getattr(app.state, "upstox_token", None))
                        return bool(token)
                    creds, _src = resolve_alpaca_data_credentials(
                        getattr(app.state, "alpaca_data_store", None), settings
                    )
                    return bool(creds.get("api_key") and creds.get("api_secret"))
                except (RuntimeError, ValueError, OSError, AttributeError):
                    return False

            def _scan_limits(market: str) -> dict[str, Any]:
                out: dict[str, Any] = {}
                try:
                    alloc = app.state.ensemble_service.allocators[market].config.to_dict()
                except (KeyError, AttributeError):
                    alloc = {}
                try:
                    _asdict = _dataclasses.asdict(app.state.live_paper_engine.configs[market])
                except (KeyError, AttributeError):
                    _asdict = {}
                out["ensemble_allocator"] = alloc
                out["paper_risk_gate"] = _asdict
                out["note"] = (
                    "Unchanged by scanning: a 100-symbol scan cannot open more "
                    "positions, exceed exposure, or place extra orders."
                )
                return out

            def _scan_watchlist(market: str) -> list[str]:
                try:
                    rows = app.state.live_paper_engine.list_watchlists().get(market) or []
                except (KeyError, AttributeError, OSError):
                    return []
                return [str(row.get("symbol", "")).upper() for row in rows if row.get("symbol")]

            def _scan_evaluate(market: str, **kwargs: Any) -> dict[str, Any]:
                kwargs["execute_in_paper"] = False
                kwargs["persist_decisions"] = False
                return app.state.ensemble_service.evaluate_watchlist_cycle(market, **kwargs)

            _nse_profile = "upstox_standard" if _quota_on else "off"
            _nasdaq_profile = f"alpaca_{_alpaca_plan}" if _quota_on else "off"
            _nasdaq_plan_detail = (
                "Basic (free) market-data profile assumed (200 requests/min). No paid Alpaca plan "
                "is inferred, and the subscription entitlement is not verified by this app."
                if _alpaca_plan == "basic"
                else f"Operator-declared plan {_alpaca_plan.upper()!r} sets the limits below; the "
                "entitlement is NOT verified against the Alpaca account."
            )
            app.state.scan_capacity_service = ScanCapacityService(
                gates=_fetch_gates,
                policies={
                    "NSE": upstox_standard_policies() if _quota_on else (),
                    "NASDAQ": alpaca_market_data_policies(_alpaca_plan) if _quota_on else (),
                },
                profiles={"NSE": _nse_profile, "NASDAQ": _nasdaq_profile},
                plan_states={
                    "NSE": (
                        "verified",
                        "Upstox standard API windows: 50/s, 500/min, 2,000/30 min per endpoint "
                        "category (per-API, per-user).",
                    ),
                    "NASDAQ": ("unverified", _nasdaq_plan_detail),
                },
                credential_present=_scan_credential_present,
                evaluate=_scan_evaluate,
                limits=_scan_limits,
                watchlist=_scan_watchlist,
            )
            # Live execution is a SEPARATE path from local paper. It starts fully
            # disarmed: gates are in-memory, per market, and require explicit
            # mode=LIVE + readiness + Arm + phrase-confirmed Activate.
            from app.markets.live_execution import (
                LiveExecutionCoordinator,
                LiveRiskLimits,
                MultiMarketLiveRunner,
                NasdaqLiveAdapter,
                NseLiveAdapter,
            )
            from app.markets.us_calendar import USEquitiesCalendar

            def _nse_live_token_value() -> str:
                try:
                    return resolve_live_token(getattr(app.state, "upstox_live_token", None))[0]
                except (RuntimeError, ValueError):
                    return ""

            def _nse_live_adapter() -> NseLiveAdapter:
                catalog = (
                    app.state.autonomy.catalog
                    if (app.state.autonomy is not None and app.state.autonomy.catalog.instruments)
                    else app.state.nse_seed_catalog
                )
                return NseLiveAdapter(
                    catalog,
                    live_token_provider=_nse_live_token_value,
                    algo_name=str(getattr(autonomy_settings, "upstox_algo_name", "") or ""),
                    live_enabled=bool(getattr(autonomy_settings, "live_enabled", False)),
                    expected_public_ip=str(getattr(autonomy_settings, "expected_public_ip", "") or ""),
                    quote_provider=_nse_live_quote_lookup,
                    require_fresh_catalog=True,
                )

            def _nasdaq_live_adapter() -> NasdaqLiveAdapter:
                return NasdaqLiveAdapter(
                    lambda: getattr(getattr(app.state, "alpaca_broker_runtime", None), "service", None),
                    quote_provider=_us_live_quote_lookup,
                    calendar=USEquitiesCalendar(),
                )

            # All live limits start at zero => fail closed until an operator
            # explicitly stores per-market limits via /api/markets/live/limits.
            app.state.live_execution = LiveExecutionCoordinator(
                journal_path=":memory:" if runtime_base is None else (runtime_base / "live_market_execution.sqlite3"),
                adapters={"NSE": _nse_live_adapter(), "NASDAQ": _nasdaq_live_adapter()},
                limits={"NSE": LiveRiskLimits(), "NASDAQ": LiveRiskLimits()},
                paper_engine=app.state.live_paper_engine,
            )
            app.state.live_paper_engine.execution_mode_provider = app.state.live_execution.mode
            live_runner_enabled = os.environ.get("AXIOM_MM_LIVE_RUNNER_ENABLED", "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
            app.state.mm_live_runner = MultiMarketLiveRunner(
                app.state.live_execution,
                app.state.ensemble_service,
                interval_seconds=float(os.environ.get("AXIOM_MM_LIVE_INTERVAL_SEC", "15.0")),
                market_timeout_seconds=float(os.environ.get("AXIOM_MM_LIVE_TIMEOUT_SEC", "20.0")),
                enabled=live_runner_enabled,
            )
            app.state.mm_live_runner.start()
            runner_enabled = os.environ.get("AXIOM_MM_PAPER_RUNNER_ENABLED", "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
            app.state.mm_paper_runner = MultiMarketPaperRunner(
                app.state.live_paper_engine,
                interval_seconds=float(os.environ.get("AXIOM_MM_PAPER_INTERVAL_SEC", "10.0")),
                market_timeout_seconds=float(os.environ.get("AXIOM_MM_PAPER_TIMEOUT_SEC", "12.0")),
                enabled=runner_enabled,
                paper_blocked=lambda mkt: not app.state.live_execution.paper_execution_allowed(mkt),
            )
            app.state.mm_paper_runner.start()

            yield
            if getattr(app.state, "mm_paper_runner", None) is not None:
                await app.state.mm_paper_runner.stop()
            if getattr(app.state, "mm_live_runner", None) is not None:
                await app.state.mm_live_runner.stop()
            if getattr(app.state, "live_execution", None) is not None:
                app.state.live_execution.close()
            await app.state.simulator.stop(reason="APPLICATION_SHUTDOWN")
            if getattr(app.state, "ensemble_service", None) is not None:
                app.state.ensemble_service.close()
            if getattr(app.state, "live_paper_engine", None) is not None:
                app.state.live_paper_engine.close()
            if getattr(app.state, "kronos_provider", None) is not None:
                app.state.kronos_provider.stop_all_sessions(reason="application_shutdown")
            if ephemeral_model_lab_tmp is not None:
                ephemeral_model_lab_tmp.cleanup()
            if getattr(app.state, "alpaca_broker_runtime", None) is not None and app.state.alpaca_broker_runtime.journal is not None:
                app.state.alpaca_broker_runtime.journal.close()
            if app.state.autonomy_runner is not None:
                await app.state.autonomy_runner.stop()
            if app.state.autonomy is not None:
                app.state.autonomy.close()
            if autonomy_store is not None:
                autonomy_store.close()
            # Release the single-instance lease explicitly: on Windows an open
            # lock handle keeps the file locked and any later cleanup of the
            # data directory fails with a sharing violation.
            autonomy_lease = getattr(app.state, "autonomy_lease", None)
            if autonomy_lease is not None:
                autonomy_lease.release()
            app.state.market_data.disconnect("Application shutdown")
            if app.state.vantage.connector:
                app.state.vantage.connector.disconnect()
            broker.close()
        finally:
            repository.release_writer()
            database.close()

    app = FastAPI(
        title="Arena",
        version=__version__,
        lifespan=lifespan,
        description="Local paper plus NSE (Upstox) and NASDAQ (Alpaca) multi-market operator console; startup is paper-only and disarmed.",
        docs_url="/docs",
        redoc_url=None,
    )

    app.state.api_latencies = []  # legacy rolling last-100 request latencies (ms)
    app.state.performance = LatencyTracker(capacity=512)

    @app.middleware("http")
    async def record_latency(request, call_next):
        import time

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        try:
            latencies = request.app.state.api_latencies
            latencies.append(round(elapsed_ms, 2))
            del latencies[:-100]  # bounded memory
            request.app.state.performance.observe("http.request", elapsed_ms)
        except Exception:
            pass  # metrics must never break a request
        response.headers["x-api-latency-ms"] = f"{elapsed_ms:.2f}"
        return response
    app.state.settings = settings
    app.state.data_dir = Path(data_dir or ROOT / "data")
    app.state.explicit_data_dir = data_dir is not None
    app.state.research_lock = Lock()
    app.state.listed_assets = ListedAssetService()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # FastAPI's default validation payload includes each error's raw input.
        # That could echo a malformed broker password. Return only safe schema
        # locations/messages; never raw input, context, or exception strings.
        errors = [
            {"loc": list(error.get("loc", ())), "msg": error.get("msg", "Invalid input"),
             "type": error.get("type", "value_error")}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(OrderBlocked)
    async def order_blocked(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc), "issues": exc.issues})

    @app.exception_handler(ServiceError)
    async def service_refused(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(MarketDataError)
    async def market_data_refused(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(BrokerError)
    async def broker_refused(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ContractViolation)
    async def contract_refused(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(SecretStoreUnavailable)
    async def secrets_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(MT5PackageUnavailable)
    async def mt5_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ListedProviderUnavailable)
    async def listed_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_input(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger("bot.api").error("request_failed", extra={"error": type(exc).__name__})
        return JSONResponse(
            status_code=500, content={"detail": "Internal error; review local logs"}
        )

    app.include_router(health.router)
    app.include_router(autonomy.page)
    app.include_router(autonomy.api)
    app.include_router(terminal.router)
    # Dashboard router applies its own per-route dependencies: the HTML PAGE
    # is readable by local (loopback) browsers without a Bearer header, while
    # /dashboard/data and the engine STOP/RESUME controls keep the full
    # bearer gate (see app/api/auth.py).
    app.include_router(dashboard.router)
    # Chart terminal: the PAGE keeps the loopback-open contract of /dashboard,
    # every data route declares its own bearer dependency.
    app.include_router(chart.router)
    for module in (
        market, strategies, backtests, paper, v2, market_events,
        market_data, listed_assets, connections, alpaca_broker, markets, model_lab, ensemble,
        performance_routes, scan_capacity,
    ):
        app.include_router(module.router, dependencies=[Depends(authenticate)])
    # Static assets for the chart terminal. Local files only: no CDN, no
    # remote script, and nothing secret is ever placed here. Assets are served
    # with no-store so an upgrade can never be masked by a browser-cached UI.
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def no_store_static(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response
    app.state.sandbox_engine = None  # set by the sandbox engine embedding
    app.state.sandbox_broker = None
    return app
