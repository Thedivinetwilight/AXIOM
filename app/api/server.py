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
from app.api.routes import console as console_routes
from app.api.routes import setup as setup_routes
from app.api.routes import frontend as frontend_routes
from app.api.routes import wizard as wizard_routes
from app.api.routes import app_api as app_api_routes
from app.api.routes import workstation as workstation_routes

logger = logging.getLogger("bot.api")


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
            # AXIOM-MONEY-PRINTER workstation services share this repository.
            from app import workstation as workstation_module

            configured = Path(settings.data_dir)
            if not configured.is_absolute():
                configured = ROOT / configured
            app.state.workstation = workstation_module.build(
                settings, repository, Path(data_dir) if data_dir else configured)
            try:
                app.state.workstation.tasks.recover()
            except Exception as exc:
                logger.warning("task recovery skipped at startup: %s", exc)
            # A control-plane restart never inherits live entry authority. A
            # surviving engine is told to drain, retaining its lease while any
            # positions/open orders still require risk-reducing management.
            try:
                app.state.workstation.live_sessions.recover_after_restart()
            except Exception as exc:
                # If session recovery cannot read/write durable state, abort
                # startup rather than expose a control plane that may disagree
                # with the engine about whether entries are authorized.
                logger.error("live-session startup recovery failed: %s",
                             type(exc).__name__)
                raise
            # Paper execution feeds the intelligence layer with OBSERVED bars.
            app.state.simulator.workstation = app.state.workstation
            yield
            try:
                app.state.workstation.live_sessions.stop(
                    reason="workstation API shutdown")
            except Exception as exc:
                logger.error("live-session shutdown transition failed: %s",
                             type(exc).__name__)
            try:
                close_result = app.state.workstation.live_supervisor.close(
                    timeout=5.0)
                if close_result.get("running"):
                    logger.warning(
                        "live engine retained for risk management during shutdown")
            except Exception as exc:
                logger.error("live supervisor shutdown request failed: %s",
                             type(exc).__name__)
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
    # Feature console: `loopback_or_bearer` on the HTML shell only; every
    # endpoint that EXECUTES a command carries the full bearer gate in the
    # route module itself.
    app.include_router(console_routes.router)
    # First-run setup writes credentials, so it is loopback-or-bearer like
    # the dashboard page and never open to the network.
    app.include_router(setup_routes.router)
    # 3.1.0 redesign: the guided wizard, the main application shell, static
    # assets and the application-level endpoints. Page routes apply their own
    # loopback_or_bearer dependency; protected data routes carry the bearer
    # gate explicitly in the route modules.
    app.include_router(frontend_routes.router)
    app.include_router(wizard_routes.router)
    app.include_router(app_api_routes.router)
    for module in (market, strategies, backtests, paper, v2, market_events):
        app.include_router(module.router, dependencies=[Depends(authenticate)])
    # AXIOM-MONEY-PRINTER workstation: markets/universes, opportunities,
    # evidence, live session state machine, Kronos studio, tasks, backups.
    app.include_router(workstation_routes.router)
    app.state.sandbox_engine = None  # set by the sandbox engine embedding
    app.state.sandbox_broker = None
    return app
