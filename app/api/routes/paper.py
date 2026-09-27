from fastapi import APIRouter, Query, Request
from app.api.schemas import PaperStartRequest
from app.api.services import load_dataset
from app.data.factory import stream_provider

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
    if body.timeframe != request.app.state.broker.timeframe:
        raise ValueError(
            "Paper timeframe must match DEFAULT_TIMEFRAME; restart with separate DB to change"
        )
    strategy = body.build_strategy()
    bars = load_dataset(request.app, body) if body.source == "replay" else None
    provider = stream_provider(
        request.app.state.settings,
        body.source,
        bars=bars,
        delay=body.replay_delay,
        symbol=body.symbol,
    )
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
