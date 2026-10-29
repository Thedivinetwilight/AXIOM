from contextlib import asynccontextmanager
import logging
from pathlib import Path
from threading import Lock
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
    dashboard, health, market, strategies, backtests, paper, v2, market_events, chart, vantage, terminal,
    market_data, listed_assets, connections, alpaca_broker, markets,
)
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
from app.autonomy.upstox_token import resolve_live_token_provider, resolve_token_provider
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

            from app.markets.live_paper import LiveMarketPaperEngine
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
                res = cat.search(symbol_or_key, include_indices=False, limit=1)
                return res["items"][0] if res["items"] else None

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
                return {
                    "instrument_key": snap.instrument_key,
                    "bid": snap.bid,
                    "ask": snap.ask,
                    "last": snap.last,
                    "volume": snap.volume,
                    "timestamp": snap.timestamp.isoformat(),
                    "source": "upstox-market-quote-v2",
                }

            def _nasdaq_live_quote_lookup(symbol: str):
                quote = _us_live_quote_lookup(symbol)
                if quote is None:
                    raise RuntimeError("Alpaca read-only market-data credentials are not configured")
                return quote

            app.state.live_paper_engine = LiveMarketPaperEngine(
                ":memory:" if runtime_base is None else (runtime_base / "live_market_paper.sqlite3"),
                nse_quote_provider=_nse_live_quote_lookup,
                nasdaq_quote_provider=_nasdaq_live_quote_lookup,
                nse_instrument_resolver=_resolve_nse_instrument,
                nasdaq_instrument_resolver=_resolve_nasdaq_instrument,
            )

            yield
            if getattr(app.state, "live_paper_engine", None) is not None:
                app.state.live_paper_engine.close()
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
            await app.state.simulator.stop(reason="APPLICATION_SHUTDOWN")
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
        description="Local paper plus guarded Vantage MT5 demo/live sessions; startup is disconnected and disarmed.",
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
    for module in (market, strategies, backtests, paper, v2, market_events, vantage, market_data, listed_assets, connections, alpaca_broker, markets):
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
