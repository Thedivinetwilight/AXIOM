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
)
from app.vantage.models import SessionMode
from app.vantage.connector import BrokerError
from app.vantage.gateway import MT5PackageUnavailable
from app.vantage.risk import ContractViolation
from app.vantage.secrets import DpapiSecretStore, SecretStoreUnavailable
from app.vantage.service import OrderBlocked, ServiceError, VantageService
from app.vantage.store import VantageStore


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
            )
            yield
            await app.state.simulator.stop()
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

    app.state.api_latencies = []  # rolling last-100 request latencies (ms)

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
        except Exception:
            pass  # metrics must never break a request
        response.headers["x-api-latency-ms"] = f"{elapsed_ms:.2f}"
        return response
    app.state.settings = settings
    app.state.data_dir = Path(data_dir or ROOT / "data")
    app.state.research_lock = Lock()

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
    app.include_router(terminal.router)
    # Dashboard router applies its own per-route dependencies: the HTML PAGE
    # is readable by local (loopback) browsers without a Bearer header, while
    # /dashboard/data and the engine STOP/RESUME controls keep the full
    # bearer gate (see app/api/auth.py).
    app.include_router(dashboard.router)
    # Chart terminal: the PAGE keeps the loopback-open contract of /dashboard,
    # every data route declares its own bearer dependency.
    app.include_router(chart.router)
    for module in (market, strategies, backtests, paper, v2, market_events, vantage):
        app.include_router(module.router, dependencies=[Depends(authenticate)])
    # Static assets for the chart terminal. Local files only: no CDN, no
    # remote script, and nothing secret is ever placed here.
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.state.sandbox_engine = None  # set by the sandbox engine embedding
    app.state.sandbox_broker = None
    return app
