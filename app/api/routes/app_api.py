"""Application-level endpoints for the redesigned UI (3.1.0).

* ``GET  /api/system/info``      runtime identity, uptime, database, lock state
* ``GET  /api/instance``         this session's instance-lock status
* ``POST /api/paper/start-default`` start paper with the configured defaults
* ``GET  /api/live/readiness``   live-rail prerequisite checklist (read-only)
* ``POST /api/research/validation`` chronological split / walk-forward /
                                   robustness — real computations, clearly
                                   labelled; never presented as live results
* ``POST /api/credentials/reveal`` loopback-only one-shot reveal of the local
                                   API token (parity with the setup screen's
                                   trust level; never logged, never in URLs)

All routes keep the existing authentication model: protected endpoints carry
the bearer gate; loopback conveniences carry ``loopback_or_bearer`` exactly
like the setup/dashboard pages.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import build_info
from app.api.auth import authenticate, loopback_or_bearer

router = APIRouter()

_STARTED_AT = time.monotonic()


@router.get("/api/system/info", dependencies=[Depends(authenticate)])
async def system_info(request: Request) -> dict:
    from app import runtime

    settings = request.app.state.settings
    repository = getattr(request.app.state, "repository", None)
    simulator = getattr(request.app.state, "simulator", None)
    writer = "held by this session" if repository and (
        repository._writer_file or repository._memory_writer) else "available"
    broker = (settings.broker or "LOCAL_PAPER").upper()
    live = bool(settings.live_trading) and broker == "UPSTOX_LIVE"
    display = (f"{broker} · LIVE — REAL MONEY" if live
               else f"{broker} · SANDBOX ORDERS" if broker == "UPSTOX_SANDBOX"
               else f"{broker} · SIMULATED (PAPER)")
    return {
        **build_info.describe(),
        "mode": settings.mode.upper(),
        "mode_display": display,
        "broker": broker,
        "live_trading": live,
        "uptime_seconds": round(time.monotonic() - _STARTED_AT, 1),
        "database": settings.database_url,
        "writer_lock": writer,
        "engine_state": simulator.state.value if simulator else "UNKNOWN",
        "home": str(runtime.arena_home()),
        "frozen": runtime.is_frozen(),
    }


@router.get("/api/instance", dependencies=[Depends(loopback_or_bearer)])
async def instance_status(request: Request) -> dict:
    guard = getattr(request.app.state, "instance_guard", None)
    if guard is not None:
        return guard.status()
    from app import runtime
    from app.instance import InstanceGuard

    return InstanceGuard(runtime.arena_home()).status()


class StartDefaultRequest(BaseModel):
    source: str = Field(default="replay", pattern="^(replay|upstox)$")
    replay_delay: float = Field(default=0.02, ge=0, le=60)


@router.post("/api/paper/start-default", dependencies=[Depends(authenticate)])
async def paper_start_default(body: StartDefaultRequest, request: Request):
    """Start the paper engine using the configured defaults.

    This is the one-click path the wizard and the START PAPER button use. It
    deliberately reuses the exact same engine construction as ``POST
    /paper/start`` — one implementation, no drift between the two.
    """
    from app.api.routes.paper import do_start
    from app.api.schemas import PaperStartRequest

    settings = request.app.state.settings
    source = body.source
    if source == "replay":
        symbol = settings.default_symbol or "DEMO"
        timeframe = settings.default_timeframe or "1D"
    else:
        symbols = settings.market_data_symbol_list()
        if not symbols:
            raise HTTPException(
                422, "Live-market paper needs MARKET_DATA_SYMBOLS in the configuration.")
        symbol = symbols[0]
        # The live-data adapter builds intraday bars; a daily default cannot
        # drive it. This mirrors engine_timeframe() semantics.
        timeframe = settings.default_timeframe \
            if settings.default_timeframe in ("1m", "5m", "15m", "30m", "1h") else "1m"

    payload = {
        "strategy": "moving_average",
        "parameters": {},
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset": "sample.csv",
        "source": source,
        "replay_delay": body.replay_delay,
    }
    try:
        paper_body = PaperStartRequest(**payload)
    except ValueError as exc:
        raise HTTPException(422, f"Default paper configuration invalid: {exc}")
    return await do_start(paper_body, request)


@router.get("/api/live/readiness", dependencies=[Depends(authenticate)])
async def live_readiness(request: Request) -> dict:
    """The packaged live rail's prerequisite checklist — informational only.

    Nothing here can enable live trading; it reports what the documented
    multi-switch opt-in still requires. The rail itself (authentication,
    order routing, guards, watchdog, kill switch, audit) ships inside this
    build, locked, ready for deliberate activation outside the UI.
    """
    from app.live import preflight as live_preflight

    settings = request.app.state.settings
    repository = getattr(request.app.state, "repository", None)
    checks = [check.to_dict() for check in
              live_preflight.run_checks(settings, repository=repository)]
    enabled = (bool(settings.live_trading)
               and (settings.broker or "").upper() == "UPSTOX_LIVE"
               and (settings.mode or "").upper() == "LIVE")
    return {
        "enabled": enabled,
        "checks": checks,
        "required_phrase": live_preflight.LIVE_CONFIRM_PHRASE,
        "note": (
            "Live activation is intentionally NOT available from this UI. "
            "Follow LIVE_TRADING_ENABLEMENT.md; Upstox live order traffic must "
            "originate from your registered static IP and access tokens are "
            "daily (refresh with the packaged upstox-login flow)."
        ),
    }


@router.post("/api/credentials/reveal", dependencies=[Depends(loopback_or_bearer)])
async def reveal_token(request: Request) -> dict:
    """Loopback-only one-shot reveal of the LOCAL api token.

    Same trust boundary as /setup/generate-token (loopback clients already
    receive fresh tokens there). The value is returned in the JSON body only —
    never in a URL, never in a log line.
    """
    settings = getattr(request.app.state, "settings", None)
    token = (getattr(settings, "api_token", "") or "").strip()
    if not token:
        from app import runtime
        from app.config.envfile import read_values

        if runtime.env_file().exists():
            token = (read_values(runtime.env_file()).get("API_TOKEN") or "").strip()
    if not token:
        raise HTTPException(404, "No API token is configured yet — run the setup wizard.")
    return {
        "token": token,
        "note": "Shown once. Keep it private; it protects every protected endpoint.",
    }


class ValidationRequest(BaseModel):
    kind: str = Field(pattern="^(split|walk_forward|robustness)$")
    dataset: str = Field(default="sample.csv", pattern=r"^[A-Za-z0-9_-]+\.csv$")
    symbol: str = "DEMO"
    timeframe: str = "1D"
    strategy: str = "moving_average"


@router.post("/api/research/validation", dependencies=[Depends(authenticate)])
async def research_validation(body: ValidationRequest, request: Request):
    """Chronological validation computed by the real research modules.

    * split         — chronological train/validation/test partition sizes
    * walk_forward  — rolling discovery/selection over disjoint test folds
    * robustness    — parameter/cost sensitivity of a fixed candidate

    These are RESEARCH computations on historical data. They never trade and
    their results are never equivalent to live performance.
    """
    from app.api.services import load_dataset
    from app.backtesting.costs import ExecutionConfig
    from app.backtesting.engine import BacktestEngine
    from app.strategies import create_strategy

    lock = request.app.state.research_lock
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "A research job is already running")
    try:
        bars = load_dataset(request.app, body)
        engine = BacktestEngine(ExecutionConfig())

        if body.kind == "split":
            from app.validation.train_test import SplitConfig, split_data

            train, validation, test = split_data(bars, SplitConfig(), body.timeframe)
            return {
                "kind": "split",
                "sizes": {"train": len(train), "validation": len(validation), "test": len(test)},
                "note": "Chronological partition — no shuffling; test data is out-of-sample.",
            }

        if body.kind == "walk_forward":
            from app.validation.walk_forward import WalkForwardConfig, walk_forward

            # Standard windows when the dataset carries them; smaller honest
            # windows otherwise (never overlapping test folds).
            if len(bars) >= 360 + 120 + 120:
                config = WalkForwardConfig()
            elif len(bars) >= 120 + 40 + 40:
                config = WalkForwardConfig(train_bars=120, validation_bars=40,
                                           test_bars=40, step_bars=40)
            else:
                raise HTTPException(
                    422,
                    f"Walk-forward needs at least 200 bars; the dataset has {len(bars)}.",
                )
            result = walk_forward(bars, body.timeframe, engine, config=config)
            return {
                "kind": "walk_forward",
                "folds": len(result["folds"]),
                "aggregate": result["aggregate_metrics"],
                "warnings": result["warnings"]
                + [w for fold in result["folds"] for w in fold.get("warnings", [])],
                "note": "Out-of-sample aggregate across disjoint test windows.",
            }

        # robustness
        from app.validation.robustness import robustness

        strategy = create_strategy(body.strategy, {})
        candidate = {"strategy": strategy.name, "parameters": dict(strategy.parameters)}
        result = robustness(bars, body.timeframe, candidate, engine)
        baseline = result["baseline"]
        trials = result["trials"]
        base_return = baseline.get("total_return")
        sensitive = any(
            abs(trial.get("return_delta", 0)) > 0.15
            for trial in trials
            if str(trial.get("scenario", "")).startswith("parameter:")
        ) if base_return is not None else False
        return {
            "kind": "robustness",
            "baseline": baseline,
            "trials": len(trials),
            "sensitive": sensitive,
            "warnings": result["warnings"],
            "note": "Sensitivity diagnostics — do not tune parameters to these results.",
        }
    finally:
        lock.release()
