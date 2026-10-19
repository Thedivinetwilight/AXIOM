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
from app.config.envfile import read_values, restore_snapshot, update_values

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


def _proposed_settings(current, updates: dict):
    """The Settings this wizard write would produce, WITHOUT touching disk.

    Used to validate the whole change first. A refused change must leave both
    `.env` and the running engine exactly as they were.
    """
    from dataclasses import replace

    mapping = {
        "BROKER": "broker",
        "MODE": "mode",
        "API_TOKEN": "api_token",
        "DATA_SOURCE": "data_source",
        "DEFAULT_SYMBOL": "default_symbol",
        "DEFAULT_TIMEFRAME": "default_timeframe",
        "MARKET_DATA_ACCESS_TOKEN": "market_data_access_token",
        "MARKET_DATA_SYMBOLS": "market_data_symbols",
    }
    kwargs = {mapping[key]: value for key, value in updates.items()
              if key in mapping}
    return replace(current, **kwargs)


def _refuse_incompatible_paper(candidate) -> None:
    """Read-only check that this change fits the existing paper account."""
    from app import runtime
    from app.maintenance.paper_recovery import recovery_command
    from app.paper_trading.compatibility import (
        PaperAccountMismatch,
        PaperDatabaseUnreadable,
        ensure_compatible,
    )

    try:
        ensure_compatible(candidate, env_file=runtime.env_file(),
                          recovery_command=recovery_command())
    except PaperAccountMismatch as exc:
        raise HTTPException(
            409,
            exc.guidance(env_file=runtime.env_file(),
                         recovery_command=recovery_command())
            + "\n\nNothing was saved.",
        ) from None
    except PaperDatabaseUnreadable as exc:
        raise HTTPException(409, f"{exc}\n\nNothing was saved.") from None


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
        md_symbols_raw = (payload.market_data_symbols or "").strip()
        if not md_token or not md_symbols_raw:
            raise HTTPException(422, "Live-market paper needs a market-data token and instrument keys.")
        # Validate instrument keys BEFORE touching the network or writing to
        # disk. A typo like "NSE_EQ:INE002A01018" should be rejected with a
        # useful message rather than silently persisted (the engine later
        # rejects it at stream creation, producing a cryptic error).
        try:
            md_symbols_list = _validate_upstox_instrument_keys(md_symbols_raw)
        except ValueError as exc:
            raise HTTPException(422, f"Instrument key invalid: {exc}") from exc
        md_symbols = ",".join(md_symbols_list)
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
            "DEFAULT_SYMBOL": md_symbols_list[0],
            "DEFAULT_TIMEFRAME": "1m",
        })

    # A last line of defense: the wizard's own write list can never carry a
    # live switch, even if future code grows careless.
    for key in updates:
        if key in LIVE_ENABLEMENT or key.startswith("RISK_"):
            raise HTTPException(500, "Internal error: wizard attempted a live key.")
    if updates["BROKER"] in LIVE_BROKERS:
        raise HTTPException(500, "Internal error: wizard attempted a live broker.")

    # Validate the whole change BEFORE writing anything. The previous order
    # wrote .env and closed the old broker first, so a timeframe that did not
    # match the saved paper account left a refused 409, a rewritten
    # configuration and a closed broker behind.
    previous = request.app.state.settings
    try:
        candidate = _proposed_settings(previous, updates)
    except ValueError as exc:
        raise HTTPException(
            422, f"The proposed paper configuration is invalid: {exc}. "
                 "Nothing was saved.") from None
    _refuse_incompatible_paper(candidate)

    env_path = runtime.env_file()
    before_bytes = env_path.read_bytes() if env_path.exists() else None
    summary = update_values(env_path, updates)
    try:
        _rebuild_engine_for(request, candidate)
    except Exception as exc:
        # Restore the exact pre-request bytes, then surface the refusal. The
        # old broker is restored by `_rebuild_engine_for` itself when possible.
        # Replaying only the touched keys could leave a stray blank line (or
        # create a file that did not exist), which would make "nothing was
        # saved" untrue.
        try:
            restore_snapshot(env_path, before_bytes)
        except OSError:
            pass  # the original failure is the one worth reporting
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            500,
            "The configuration file was updated but the running engine could "
            f"not be rebuilt ({type(exc).__name__}); it was rolled back. "
            "Restart Arena and review the log.",
        ) from exc

    # Only after a successful rebuild: make the new settings authoritative for
    # every route in THIS process. `os.environ` is deliberately not mutated:
    # the .env file is the durable source, and mutating the process
    # environment is how a test (or a second wizard call) used to leak one
    # configuration into an unrelated process.
    request.app.state.settings = candidate
    init = _initialize_home(candidate)

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
    """Align the in-process paper engine with a VALIDATED settings change.

    A fresh install needs no restart at all — the point of the wizard. The
    broker's timeframe is fixed at construction, so a changed DEFAULT_TIMEFRAME
    requires rebuilding it:

    * only when the engine is NOT running (never mid-stream);
    * the compatibility check runs BEFORE the old broker is closed, so a
      mismatch cannot leave the app with a closed broker and a rewritten .env;
    * the old broker is persisted and its writer released first;
    * if construction still fails (lock, I/O), the previous broker is reopened
      so the running application remains usable and the caller can roll .env
      back honestly.
    """
    broker = getattr(request.app.state, "broker", None)
    simulator = getattr(request.app.state, "simulator", None)
    if broker is None or broker.timeframe == settings.default_timeframe:
        return
    if simulator is not None and simulator.task and not simulator.task.done():
        raise HTTPException(
            409, "The paper engine is running; stop it before changing the timeframe.")

    # Read-only refusal FIRST — the old broker is still open and untouched.
    _refuse_incompatible_paper(settings)

    repository = getattr(request.app.state, "repository", None) or broker.repository
    old_config = broker.config
    old_currency, old_timeframe = broker.currency, broker.timeframe
    old_calendar = broker.calendar

    from app.backtesting.costs import ExecutionConfig
    from app.paper_trading.broker import PaperBroker
    from app.paper_trading.execution import make_calendar
    from app.paper_trading.simulator import PaperSimulator

    broker.close()  # persist the OLD account, release the writer lock
    broker.repository = None  # detach: lifespan must not re-persist it
    try:
        new_broker = PaperBroker(
            ExecutionConfig(**settings.execution_kwargs()),
            currency=settings.currency,
            timeframe=settings.default_timeframe,
            repository=repository,
            calendar=make_calendar(settings.market_calendar,
                                   settings.default_timeframe),
        )
    except Exception as exc:
        restored_note = "the previous paper engine could not be reopened"
        try:
            if repository is not None:
                restored = PaperBroker(
                    old_config,
                    currency=old_currency,
                    timeframe=old_timeframe,
                    repository=repository,
                    calendar=old_calendar,
                )
                request.app.state.broker = restored
                request.app.state.simulator = PaperSimulator(restored)
                restored_note = "the previous paper engine was restored"
        except Exception as restore_exc:
            restored_note = (
                "the previous paper engine could NOT be restored "
                f"({type(restore_exc).__name__}); restart Arena")
        raise HTTPException(
            409,
            f"The existing paper account cannot be opened with the proposed "
            f"configuration ({type(exc).__name__}: {exc}). Nothing was saved; "
            f"{restored_note}.",
        ) from exc
    request.app.state.broker = new_broker
    request.app.state.simulator = PaperSimulator(new_broker)


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
