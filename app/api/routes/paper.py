from copy import deepcopy
import json

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import ManualOrderRequest, PaperStartRequest
from app.api.services import load_dataset
from app.data.factory import stream_provider
from app.paper_trading.broker import PaperBroker
from app.paper_trading.manual import (
    ManualLimits,
    ManualOrderRejected,
    place_order,
    ticket,
)

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


@router.post("/api/paper/start-default")
async def start_default(request: Request):
    """One click: start the paper engine from the configured settings.

    Used by the chart terminal's empty state so "there is no data yet" has a
    single, obvious next action. It is the same ``PaperSimulator.start`` call
    the explicit route makes, with the symbol/timeframe/source taken from
    configuration, and it inherits the process-wide PAPER-only guarantees.
    """
    settings = request.app.state.settings
    broker = _require_local_paper(request)
    source = str(getattr(settings, "data_source", "replay") or "replay").lower()
    if source not in {"replay", "upstox", "generic", "binance", "alpaca"}:
        raise HTTPException(
            409,
            f"DATA_SOURCE={source} is not a paper market-data source; "
            "configure replay/upstox/generic/binance/alpaca",
        )
    symbol = (settings.default_symbol or "").strip().upper()
    if source == "alpaca" and symbol not in settings.alpaca_symbol_list():
        # The feed subscribes to ALPACA_SYMBOLS; start the first configured one
        # rather than asking the user to retype the list.
        configured = settings.alpaca_symbol_list()
        if not configured:
            raise HTTPException(409, "ALPACA_SYMBOLS is empty; name at least one ticker")
        symbol = configured[0]
    if not symbol:
        raise HTTPException(409, "DEFAULT_SYMBOL is not configured")
    timeframe = settings.default_timeframe or broker.timeframe
    body = PaperStartRequest(
        source=source, symbol=symbol, timeframe=timeframe,
        strategy="moving_average", dataset="sample.csv", replay_delay=0.02,
    )
    return await start(body, request)


@router.post("/paper/start")
async def start(body: PaperStartRequest, request: Request):
    _require_local_paper(request)
    if body.timeframe != request.app.state.broker.timeframe:
        raise ValueError(
            "Paper timeframe must match DEFAULT_TIMEFRAME; restart with separate DB to change"
        )
    settings = request.app.state.settings
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
    if body.source == "vantage_mt5":
        from app.market_data.paper_provider import MT5PaperProvider

        market_data = getattr(request.app.state, "market_data", None)
        if market_data is None:
            raise HTTPException(409, "Independent MT5 market data is unavailable")
        account = market_data.account or {}
        if (
            account.get("currency")
            and str(account["currency"]).upper() != str(request.app.state.broker.currency).upper()
        ):
            raise HTTPException(
                409,
                "Paper currency does not match the authenticated demo account currency; "
                "configure the same CURRENCY and use a separate paper database before starting",
            )
        try:
            provider = MT5PaperProvider(market_data, body.symbol, body.timeframe)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        provider = stream_provider(
            settings,
            body.source,
            bars=bars,
            delay=body.replay_delay,
            symbol=body.symbol,
            timeframe=body.timeframe if body.source in {"upstox", "alpaca"} else None,
        )
    if body.source == "alpaca" and getattr(provider, "supported_timeframes", None) != frozenset({"1m"}):
        raise ValueError("Alpaca paper provider timeframe contract mismatch")
    # Live adapters expose the exact symbols they subscribe to; keep controller,
    # broker and feed in agreement to avoid unsubscribed-symbol failures later.
    symbols = list(getattr(provider, "symbols", None) or [body.symbol])
    if body.symbol not in symbols:
        raise ValueError("Requested symbol is not in the configured market-data instrument list")
    try:
        if body.source == "vantage_mt5":
            # Readiness completes before STARTING is returned. Failure closes
            # only this new provider; it never calls simulator.stop/cancel_all.
            provider.prepare()
        result = await request.app.state.simulator.start(provider, strategy, symbols)
    except Exception:
        if body.source == "vantage_mt5":
            await provider.close()
        raise
    if body.source == "vantage_mt5":
        repository = getattr(request.app.state, "repository", None)
        if repository is not None:
            repository.record_system_event("PAPER_MARKET_DATA_STARTED", {
                "provider": "VANTAGE_DEMO_MT5", "symbol": body.symbol,
                "timeframe": body.timeframe, "strategy": "manual_only",
                "execution_mode": "AXIOM_PAPER", "automation_enabled": False,
            })
        result["execution_mode"] = "AXIOM_PAPER"
        result["automation_enabled"] = False
    return result


@router.post("/paper/stop")
async def stop(request: Request):
    return await request.app.state.simulator.stop()


@router.post("/paper/pause")
async def pause(request: Request):
    return await request.app.state.simulator.pause()


@router.post("/paper/resume")
async def resume(request: Request):
    _require_local_paper(request)
    return await request.app.state.simulator.resume()


def _require_local_paper(request: Request, *, allow_during_emergency: bool = False) -> PaperBroker:
    """Manual trading is only ever the local paper simulator.

    Three independent conditions, all required: the process is in PAPER mode,
    live trading is off, and the broker is the in-process paper broker. A
    sandbox or live broker configured by mistake is refused rather than used.
    """
    settings = request.app.state.settings
    broker = getattr(request.app.state, "broker", None)
    vantage = getattr(request.app.state, "vantage", None)
    if vantage is not None:
        from app.vantage.models import SessionMode
        if vantage.mode != SessionMode.PAPER:
            raise HTTPException(409, "Local paper orders are disabled while a Vantage session is selected")
        if vantage.emergency_stop and not allow_during_emergency:
            raise HTTPException(
                409,
                "Durable emergency stop is set; paper entries and strategy resume remain blocked until explicitly cleared",
            )
    mode = str(getattr(settings, "mode", "")).upper()
    broker_mode = str(getattr(settings, "broker", "")).upper()
    if mode != "PAPER" or bool(getattr(settings, "live_trading", False)):
        raise HTTPException(
            409, "Manual orders require MODE=PAPER and LIVE_TRADING=false"
        )
    if broker_mode != "LOCAL_PAPER" or not isinstance(broker, PaperBroker):
        raise HTTPException(
            409,
            "Manual orders require BROKER=LOCAL_PAPER; this process has "
            f"{broker_mode or 'no broker'} configured",
        )
    return broker


def _requires_mt5_fresh_feed(request: Request) -> bool:
    broker = request.app.state.broker
    return bool(broker.context.get("market_data", {}).get("requires_fresh_live"))


def _paper_symbol(request: Request, value: str) -> str:
    symbol = value.strip()
    if _requires_mt5_fresh_feed(request):
        # Never uppercase or otherwise substitute an exact MT5 broker symbol.
        return symbol
    return symbol.upper()


def _mt5_entry_block(request: Request, symbol: str) -> str | None:
    if not _requires_mt5_fresh_feed(request):
        return None
    market_data = getattr(request.app.state, "market_data", None)
    if market_data is not None:
        reason = market_data.entry_block_reason(symbol)
        if reason:
            return reason
    simulator = request.app.state.simulator
    state = getattr(simulator.state, "value", str(simulator.state))
    if state != "RUNNING":
        return (
            f"Fresh MT5 paper session is {state}; new entries require a running, "
            "authenticated fresh-feed session"
        )
    provider = simulator.provider
    guard = getattr(provider, "entry_block_reason", None)
    if not callable(guard) or not getattr(provider, "requires_fresh_live", False):
        return "The active provider is not the authenticated MT5 fresh-feed provider"
    return guard(symbol)


def _manual_fingerprint(body: ManualOrderRequest, symbol: str) -> str:
    values = body.model_dump(exclude={"idempotency_key"})
    values["symbol"] = symbol
    return json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _duplicate_manual_result(broker, key: str | None, fingerprint: str):
    if not key:
        return None
    saved = broker.context.get("manual_idempotency", {}).get(key)
    if saved is None:
        return None
    if saved.get("fingerprint") != fingerprint:
        raise HTTPException(409, "Idempotency key was already used for a different paper order")
    result = deepcopy(saved["result"])
    result["duplicate"] = True
    return result


@router.get("/paper/ticket")
async def paper_ticket(request: Request, symbol: str = Query(min_length=1, max_length=40)):
    """Price, headroom and blockers the ticket should show for `symbol`."""
    broker = _require_local_paper(request, allow_during_emergency=True)
    resolved_symbol = _paper_symbol(request, symbol)
    result = ticket(
        broker,
        resolved_symbol,
        ManualLimits.from_settings(request.app.state.settings),
    )
    result["entry_blocked"] = _mt5_entry_block(request, resolved_symbol)
    result["market_data_required"] = _requires_mt5_fresh_feed(request)
    return result


@router.post("/paper/orders/{order_id}/cancel")
async def cancel_paper_order(order_id: str, request: Request):
    """Cancel a local simulated working order; never routes to a broker."""
    broker = _require_local_paper(request, allow_during_emergency=True)
    try:
        result = broker.cancel_order(order_id)
    except KeyError as exc:
        raise HTTPException(404, "Paper order not found") from exc
    repository = getattr(request.app.state, "repository", None)
    if repository is not None:
        repository.record_system_event("MANUAL_PAPER_ORDER_CANCELLED", {
            "order_id": order_id, "status": result["status"], "simulated": True,
        })
    return {"order": result, "simulated": True, "paper_only": True, "live_trading": False}


@router.post("/paper/order")
async def paper_order(body: ManualOrderRequest, request: Request):
    """Place one hand-entered order in the local paper broker.

    The response states `simulated: true` on every path: there is no branch
    that could route this order anywhere else.
    """
    # A durable emergency blocks new exposure, not a manually confirmed exit
    # from an existing simulated position.  Starting/resuming strategy entry
    # remains blocked by the default guard above.
    broker = _require_local_paper(request, allow_during_emergency=body.side == "SELL")
    limits = ManualLimits.from_settings(request.app.state.settings)
    symbol = _paper_symbol(request, body.symbol)
    fingerprint = _manual_fingerprint(body, symbol)
    duplicate = _duplicate_manual_result(broker, body.idempotency_key, fingerprint)
    if duplicate is not None:
        return duplicate
    if _requires_mt5_fresh_feed(request) and not body.idempotency_key:
        raise HTTPException(409, "Fresh-MT5 manual paper orders require an idempotency key")
    try:
        if body.side == "BUY":
            feed_block = _mt5_entry_block(request, symbol)
            if feed_block:
                raise ManualOrderRejected("market_data_unavailable", feed_block)
        result = place_order(
            broker,
            symbol=symbol,
            side=body.side,
            quantity=body.quantity,
            notional=body.notional,
            order_type=body.order_type,
            limit_price=body.limit_price,
            limits=limits,
            idempotency_key=body.idempotency_key,
            request_fingerprint=fingerprint,
        )
    except ManualOrderRejected as exc:
        repository = getattr(request.app.state, "repository", None)
        if repository is not None:
            # Recorded for the audit trail, with no secrets and no untrusted text.
            repository.record_system_event(
                "MANUAL_ORDER_REJECTED",
                {
                    "symbol": symbol,
                    "side": body.side,
                    "reason": exc.reason,
                    "quantity": body.quantity,
                    "notional": body.notional,
                },
            )
        raise
    repository = getattr(request.app.state, "repository", None)
    if repository is not None:
        repository.record_system_event(
            "MANUAL_ORDER_SIMULATED",
            {
                "symbol": result["order"]["symbol"],
                "side": result["order"]["side"],
                "status": result["status"],
                "quantity": result["order"]["quantity"],
                "idempotency_key": body.idempotency_key,
                "quote_source": result["order"].get("quote_source"),
                "quote_timestamp": result["order"].get("quote_timestamp"),
                "quote_id": result["order"].get("quote_id"),
            },
        )
    return result
