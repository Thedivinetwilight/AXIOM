from contextlib import asynccontextmanager
import logging
from pathlib import Path
from threading import Lock
from fastapi import Depends, FastAPI
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
from app.api.routes import dashboard, health, market, strategies, backtests, paper, v2, market_events


def create_app(settings: Settings | None = None, data_dir: Path | None = None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        database = Database(settings.database_url)
        repository = Repository(database)
        try:
            broker = PaperBroker(
                ExecutionConfig(**settings.execution_kwargs()),
                currency=settings.currency,
                timeframe=settings.default_timeframe,
                repository=repository,
                calendar=make_calendar(settings.market_calendar, settings.default_timeframe),
            )
            app.state.repository, app.state.broker = repository, broker
            app.state.simulator = PaperSimulator(broker)
            yield
            await app.state.simulator.stop()
            broker.close()
        finally:
            repository.release_writer()
            database.close()

    app = FastAPI(
        title="Arena",
        version=__version__,
        lifespan=lifespan,
        description=("Research, simulation, sandbox orders, and gated real-money "
                     "routing. Execution mode is reported truthfully by /health."),
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
    # Dashboard router applies its own per-route dependencies: the HTML PAGE
    # is readable by local (loopback) browsers without a Bearer header, while
    # /dashboard/data and the engine STOP/RESUME controls keep the full
    # bearer gate (see app/api/auth.py).
    app.include_router(dashboard.router)
    for module in (market, strategies, backtests, paper, v2, market_events):
        app.include_router(module.router, dependencies=[Depends(authenticate)])
    app.state.sandbox_engine = None  # set by the sandbox engine embedding
    app.state.sandbox_broker = None
    return app
