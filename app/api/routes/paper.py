from fastapi import APIRouter, Query, Request
from app.api.schemas import PaperStartRequest
from app.api.services import load_dataset
from app.data.factory import paper_upstox_compatibility_error, stream_provider

router = APIRouter()


@router.get("/paper/status")
async def status(request: Request):
    return request.app.state.simulator.status()


@router.get("/paper/account")
async def account(request: Request):
    return request.app.state.broker.get_balance()


@router.get("/paper/positions")
async def positions(request: Request):
    return {"positions": request.app.state.broker.get_positions()}


@router.get("/paper/orders")
async def orders(request: Request, limit: int = Query(100, ge=1, le=1000)):
    return {"orders": request.app.state.broker.get_orders()[-limit:]}


@router.get("/paper/trades")
async def trades(request: Request, limit: int = Query(100, ge=1, le=1000)):
    return {"trades": request.app.state.broker.portfolio.trades[-limit:]}


@router.post("/paper/start")
async def start(body: PaperStartRequest, request: Request):
    return await do_start(body, request)


async def do_start(body: PaperStartRequest, request: Request):
    """The one paper-start implementation, shared by /paper/start and the
    wizard's one-click /api/paper/start-default. One code path, no drift."""
    settings = request.app.state.settings
    if (str(getattr(settings, "mode", "PAPER")).upper() == "LIVE"
            or str(getattr(settings, "broker", "LOCAL_PAPER")).upper() == "UPSTOX_LIVE"):
        # The installation is in live mode: the paper engine would read the
        # live configuration (data source, timeframe, instruments) and would
        # not be the paper engine at all. Refuse with a clear reason instead
        # of failing later with a confusing data error.
        raise ValueError(
            "This installation is configured for LIVE trading; the paper "
            "engine is not available. Return to PAPER from the live switch "
            "first."
        )
    if body.timeframe != request.app.state.broker.timeframe:
        raise ValueError(
            "Paper timeframe must match DEFAULT_TIMEFRAME; restart with separate DB to change"
        )
    if body.source == "upstox":
        compatibility_error = paper_upstox_compatibility_error(
            body.timeframe, settings.market_data_mode)
        if compatibility_error:
            # Refuse before constructing the async simulator task. Never
            # silently substitute the supervised engine's fallback timeframe.
            raise ValueError(compatibility_error)
    if body.source == "alpaca":
        if str(getattr(settings, "mode", "PAPER")).upper() != "PAPER" or bool(
            getattr(settings, "live_trading", False)
        ) or str(getattr(settings, "broker", "LOCAL_PAPER")).upper() != "LOCAL_PAPER":
            raise ValueError("Alpaca is available only as a market-data feed for local PAPER")
        if str(getattr(settings, "market", "")).lower() != "us" or str(
            getattr(settings, "currency", "")
        ).upper() != "USD":
            raise ValueError("Select MARKET=us and CURRENCY=USD for Alpaca US equities")
        from app.data.alpaca import validate_us_symbol

        validate_us_symbol(body.symbol)
    strategy = body.build_strategy()
    bars = load_dataset(request.app, body) if body.source == "replay" else None
    provider = stream_provider(
        settings,
        body.source,
        bars=bars,
        delay=body.replay_delay,
        symbol=body.symbol,
        timeframe=body.timeframe if body.source in {"upstox", "alpaca"} else None,
    )
    if body.source == "upstox":
        provider_timeframe = getattr(provider, "timeframe", None)
        if provider_timeframe != body.timeframe:
            raise ValueError(
                "Upstox paper provider timeframe must match the paper broker timeframe"
            )
    if body.source == "alpaca" and getattr(provider, "supported_timeframes", None) != frozenset({"1m"}):
        raise ValueError("Alpaca paper provider timeframe contract mismatch")
    # Live adapters expose the exact symbols they subscribe to; keep controller,
    # broker and feed in agreement to avoid unsubscribed-symbol failures later.
    symbols = list(getattr(provider, "symbols", None) or [body.symbol])
    if body.symbol not in symbols:
        raise ValueError("Requested symbol is not in the configured market-data instrument list")
    return await request.app.state.simulator.start(provider, strategy, symbols)


@router.post("/paper/stop")
async def stop(request: Request):
    return await request.app.state.simulator.stop()


@router.post("/paper/pause")
async def pause(request: Request):
    return await request.app.state.simulator.pause()


@router.post("/paper/resume")
async def resume(request: Request):
    return await request.app.state.simulator.resume()
