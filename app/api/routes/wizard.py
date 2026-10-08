"""First-run wizard backend: zero-command paper trading.

The wizard turns a fresh install into a running paper session with ONE button:

* ``POST /api/wizard/prepare-paper`` — generates the local API credential,
  writes a paper-only configuration, initializes the data directories and the
  database, and hot-applies the settings to the running server. No restart,
  no copy/paste, no commands.
* ``POST /api/wizard/validate-market-data`` — read-only Upstox probe for the
  LIVE-MARKET-DATA paper mode (simulated orders, real prices).
* ``GET  /api/wizard/preflight`` — honest readiness checks for every subsystem
  the paper engine needs.

Security model is unchanged, never weakened:

* every route is loopback-or-bearer (first run binds to 127.0.0.1);
* live-enablement keys and live broker values are REFUSED here exactly like
  ``/setup/save`` — a wizard cannot become a one-click path to live trading;
* the generated token is returned exactly once, to the loopback caller, and is
  never written to any log, response detail, or URL.
"""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import loopback_or_bearer
from app.config.envfile import read_values, update_values

router = APIRouter()

# Anything the wizard refuses to write, mirroring app/api/routes/setup.py.
LIVE_ENABLEMENT = {"LIVE_TRADING", "LIVE_ARMED", "LIVE_TOKEN_MAX_AGE_HOURS", "ALLOW_LIVE"}
LIVE_BROKERS = {"UPSTOX_LIVE", "LIVE", "UPSTOX_LIVE_ENHANCED"}


class PreparePaperRequest(BaseModel):
    data_source: str = Field(default="replay", pattern="^(replay|upstox)$")
    market_data_access_token: str | None = None
    market_data_symbols: str | None = None


class ValidateMarketDataRequest(BaseModel):
    token: str = Field(min_length=1, max_length=2000)
    symbols: str = Field(min_length=1, max_length=2000)


def _reload_settings(request: Request):
    """Re-read the .env into THIS process so the wizard needs no restart.

    The file is authoritative: every key it defines overrides the inherited
    environment. Secrets stay in-process — nothing here logs values.
    """
    from app import runtime
    from app.config.settings import Settings

    path = runtime.env_file()
    try:
        values = read_values(path)
    except OSError as exc:
        raise HTTPException(500, f"Configuration file unreadable: {type(exc).__name__}")
    for key, value in values.items():
        os.environ[key.upper()] = value
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise HTTPException(422, f"Saved configuration rejected on reload: {exc}")
    request.app.state.settings = settings
    return settings


def _assert_paper_possible(request: Request) -> None:
    """Fail closed if switching this process to paper would be unsafe."""
    values = {}
    from app import runtime

    if runtime.env_file().exists():
        values = read_values(runtime.env_file())
    broker = (values.get("BROKER") or "").upper()
    mode = (values.get("MODE") or "").upper()
    # The wizard creates LOCAL_PAPER configurations ONLY. Any other broker —
    # sandbox included — was chosen deliberately elsewhere and is never
    # silently reconfigured by a first-run screen.
    if broker and broker != "LOCAL_PAPER":
        raise HTTPException(
            409,
            f"This Arena installation is configured for {broker}. The wizard "
            "only creates LOCAL_PAPER configurations and will not touch it. "
            "Edit the .env by hand if you meant to change that.",
        )
    if mode == "LIVE":
        raise HTTPException(409, "This installation is configured for live mode; the wizard will not touch it.")
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        running = (settings.broker or "").upper()
        if running not in ("", "LOCAL_PAPER"):
            raise HTTPException(
                409,
                f"The running engine uses {running}; the wizard only creates "
                "paper configurations.",
            )


def _initialize_home(settings) -> dict:
    """Directories + database schema, idempotent. Reports what it did."""
    from app import runtime
    from app.database.database import Database

    runtime.ensure_home()
    database = Database(settings.database_url)
    database.close()
    bundled = runtime.static_dir()  # sanity: frontend assets resolvable
    return {
        "home": str(runtime.arena_home()),
        "database": settings.database_url,
        "assets": bundled.is_dir(),
    }


@router.post("/api/wizard/prepare-paper", dependencies=[Depends(loopback_or_bearer)])
async def prepare_paper(payload: PreparePaperRequest, request: Request) -> dict:
    """Create the complete LOCAL_PAPER configuration in one step."""
    from app import runtime

    _assert_paper_possible(request)

    values = read_values(runtime.env_file()) if runtime.env_file().exists() else {}
    generated_token = None
    token = (values.get("API_TOKEN") or "").strip()
    if not token:
        generated_token = secrets.token_urlsafe(32)
        token = generated_token

    updates = {
        "BROKER": "LOCAL_PAPER",
        "MODE": "PAPER",
        "API_TOKEN": token,
    }
    if payload.data_source == "replay":
        updates.update({
            "DATA_SOURCE": "replay",
            "DEFAULT_SYMBOL": (values.get("DEFAULT_SYMBOL") or "DEMO").strip() or "DEMO",
            "DEFAULT_TIMEFRAME": (values.get("DEFAULT_TIMEFRAME") or "1D").strip() or "1D",
        })
    else:
        md_token = (payload.market_data_access_token or "").strip()
        md_symbols = (payload.market_data_symbols or "").strip()
        if not md_token or not md_symbols:
            raise HTTPException(422, "Live-market paper needs a market-data token and instrument keys.")
        check = await _probe_market_data(md_token)
        if check.verdict != "feed_ok":
            raise HTTPException(
                422,
                "The market-data credential was not accepted by Upstox "
                f"({check.verdict}). {check.guidance} Nothing was saved.",
            )
        updates.update({
            "DATA_SOURCE": "upstox",
            "MARKET_DATA_ACCESS_TOKEN": md_token,
            "MARKET_DATA_SYMBOLS": md_symbols,
            "DEFAULT_SYMBOL": md_symbols.split(",")[0].strip(),
            "DEFAULT_TIMEFRAME": "1m",
        })

    # A last line of defense: the wizard's own write list can never carry a
    # live switch, even if future code grows careless.
    for key in updates:
        if key in LIVE_ENABLEMENT or key.startswith("RISK_"):
            raise HTTPException(500, "Internal error: wizard attempted a live key.")
    if updates["BROKER"] in LIVE_BROKERS:
        raise HTTPException(500, "Internal error: wizard attempted a live broker.")

    summary = update_values(runtime.env_file(), updates)
    settings = _reload_settings(request)
    _rebuild_engine_for(request, settings)
    init = _initialize_home(settings)

    return {
        "ok": True,
        "broker": "LOCAL_PAPER",
        "mode": "PAPER",
        "data_source": payload.data_source,
        "token_generated": generated_token is not None,
        # Returned ONCE to the loopback caller so the browser can call the
        # protected API without asking the user to copy anything. Never logged.
        "token_once": generated_token,
        "env_path": summary["path"],
        "initialized": init,
        "note": (
            "Paper environment ready. The paper engine simulates every order; "
            "no broker is contacted."
        ),
    }


def _validate_upstox_instrument_keys(raw_symbols: str) -> list[str]:
    """Validate provider-form Upstox instrument keys (SEGMENT|IDENTIFIER).

    Upstox keys legitimately contain a pipe (e.g. NSE_EQ|INE002A01018)
    while Arena's internal validate_symbol charset deliberately forbids
    |. Run provider-aware validation first (rejecting malformed/empty/
    random strings), then only confirm the pipe-to-dash display form maps
    into the Arena symbol namespace so the two namespaces stay separate.
    This matches the contract enforced by app.data.upstox_stream.
    """
    from app.data.models import validate_symbol
    from app.data.upstox_stream import INSTRUMENT_KEY, to_symbol

    symbols = [item.strip() for item in raw_symbols.split(",") if item.strip()]
    if not symbols:
        raise ValueError("At least one Upstox instrument key is required")
    validated: list[str] = []
    for key in symbols:
        if not INSTRUMENT_KEY.fullmatch(key):
            raise ValueError(
                "use a valid Upstox instrument key such as NSE_EQ|INE002A01018"
            )
        try:
            validate_symbol(to_symbol(key))
        except ValueError as exc:
            raise ValueError(
                f"the Upstox key {key!r} cannot be used as an Arena symbol: {exc}"
            ) from exc
        validated.append(key)
    return validated


def _rebuild_engine_for(request: Request, settings) -> None:
    """Align the in-process paper engine with freshly saved settings.

    A fresh install needs no restart at all — the point of the wizard. The
    broker's timeframe is fixed at construction, so when the saved
    DEFAULT_TIMEFRAME changed we rebuild the broker on the spot:

    * only when the engine is NOT running (never mid-stream);
    * the old broker is persisted and its writer released first;
    * if persisted state belongs to a different account configuration the
      rebuild refuses (the repository raises) and we surface that honestly
      instead of guessing.
    """
    broker = getattr(request.app.state, "broker", None)
    simulator = getattr(request.app.state, "simulator", None)
    if broker is None or broker.timeframe == settings.default_timeframe:
        return
    if simulator is not None and simulator.task and not simulator.task.done():
        raise HTTPException(
            409, "The paper engine is running; stop it before changing the timeframe.")
    try:
        broker.close()  # persist state, release the writer lock
        broker.repository = None  # detach: lifespan must not re-persist it

        from app.backtesting.costs import ExecutionConfig
        from app.paper_trading.broker import PaperBroker
        from app.paper_trading.execution import make_calendar
        from app.paper_trading.simulator import PaperSimulator

        new_broker = PaperBroker(
            ExecutionConfig(**settings.execution_kwargs()),
            currency=settings.currency,
            timeframe=settings.default_timeframe,
            repository=request.app.state.repository,
            calendar=make_calendar(settings.market_calendar, settings.default_timeframe),
        )
        request.app.state.broker = new_broker
        request.app.state.simulator = PaperSimulator(new_broker)
    except ValueError as exc:
        raise HTTPException(
            409,
            f"The existing paper account uses a different configuration ({exc}). "
            "Restart Arena with a separate database to switch timeframes.",
        )


async def _probe_market_data(token: str):
    from app.config.settings import Settings
    from app.data.upstox_auth import verify_feed_credential

    defaults = Settings()
    return await verify_feed_credential(
        token, authorize_url=defaults.market_data_authorize_url, timeout=15.0)


@router.post("/api/wizard/validate-market-data",
             dependencies=[Depends(loopback_or_bearer)])
async def validate_market_data(payload: ValidateMarketDataRequest, request: Request) -> dict:
    """Read-only credential probe. The token is never stored or echoed."""
    try:
        symbols = _validate_upstox_instrument_keys(payload.symbols)
    except ValueError as exc:
        raise HTTPException(422, f"Instrument key invalid: {exc}") from exc

    check = await _probe_market_data(payload.token.strip())
    if check.verdict != "feed_ok":
        raise HTTPException(
            422,
            f"Upstox did not accept this market-data credential ({check.verdict}). "
            + check.guidance,
        )
    return {
        "ok": True,
        "symbol": symbols[0],
        "symbols": symbols,
        "verdict": check.verdict,
        "note": "Credential verified for Market Data Feed V3. Orders stay simulated in paper mode.",
    }


@router.get("/api/wizard/preflight", dependencies=[Depends(loopback_or_bearer)])
async def preflight(request: Request) -> dict:
    """Honest readiness checks. Nothing here is allowed to fake a READY."""
    from app import runtime

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, severity: str = "error"):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "severity": severity})

    # DATABASE
    settings = getattr(request.app.state, "settings", None)
    try:
        from app.database.database import Database

        database = Database(settings.database_url)
        database.close()
        add("DATABASE", True, settings.database_url)
    except Exception as exc:
        add("DATABASE", False, f"could not open database: {type(exc).__name__}")

    # PAPER ENGINE (writer lock must be acquirable by THIS process)
    try:
        repository = request.app.state.repository
        if repository._writer_file or repository._memory_writer:
            add("PAPER ENGINE", True, "writer lock held by this session")
        else:
            repository.acquire_writer()
            repository.release_writer()
            add("PAPER ENGINE", True, "writer lock available")
    except Exception as exc:
        add("PAPER ENGINE", False,
            f"paper writer unavailable: {exc} — close other Arena sessions using this data directory")

    # DATA
    source = getattr(settings, "data_source", "replay")
    if source == "replay":
        from app.config.settings import ROOT

        sample = ROOT / "data" / "sample.csv"
        add("DATA", sample.exists(),
            "bundled replay dataset present" if sample.exists() else "bundled sample.csv MISSING")
    else:
        has_token = bool((getattr(settings, "market_data_access_token", "") or "").strip())
        has_symbols = bool(getattr(settings, "market_data_symbol_list", lambda: [])())
        add("DATA", has_token and has_symbols,
            "market-data credential and instruments configured" if has_token and has_symbols
            else "market-data credential or instruments missing")

    # STRATEGIES
    try:
        from app.strategies import catalog

        names = [entry["name"] for entry in catalog()]
        add("STRATEGIES", bool(names), f"{len(names)} strategies registered: {', '.join(names[:4])}…")
    except Exception as exc:
        add("STRATEGIES", False, f"strategy registry failed: {type(exc).__name__}")

    # RISK
    try:
        from app.backtesting.costs import ExecutionConfig

        ExecutionConfig(**settings.execution_kwargs())
        add("RISK", True, "execution/risk configuration valid")
    except Exception as exc:
        add("RISK", False, f"risk configuration invalid: {exc}")

    # UI/API
    try:
        has_token = bool((getattr(settings, "api_token", "") or "").strip())
        add("UI / API", True,
            "API token active" if has_token else "no API token (protected endpoints disabled)")
    except Exception:
        add("UI / API", False, "could not read API state")

    # STORAGE (warn-only: frozen builds write outside the bundle)
    home = runtime.arena_home()
    add("STORAGE", os.access(str(home), os.W_OK) if home.exists() else True,
        str(home), severity="warn")

    return {"checks": checks, "ready": all(c["ok"] for c in checks)}
