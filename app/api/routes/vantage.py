"""Authenticated operator API for the mutually exclusive paper/Vantage shell."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.vantage.gateway import mt5_package_available
from app.vantage.models import OrderInput, SessionMode


router = APIRouter(prefix="/api/trading")


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ModeRequest(Schema):
    mode: Literal["AXIOM_PAPER", "VANTAGE_DEMO", "VANTAGE_LIVE"]


class CredentialRequest(Schema):
    environment: Literal["VANTAGE_DEMO", "VANTAGE_LIVE"]
    login: str = Field(min_length=1, max_length=19, pattern=r"^[0-9]+$")
    password: SecretStr = Field(min_length=1, max_length=512)
    server: str = Field(min_length=1, max_length=160)
    terminal_path: str = Field(default="", max_length=500)
    label: str = Field(default="", max_length=80)
    replace_existing: bool = False


class InstrumentRequest(Schema):
    symbols: list[str] = Field(min_length=1, max_length=50)


class RiskRequest(Schema):
    max_order_volume: float = Field(gt=0, le=1_000_000)
    max_symbol_volume: float = Field(gt=0, le=1_000_000)
    max_order_margin: float = Field(gt=0, le=1e12)
    max_total_margin: float = Field(gt=0, le=1e12)
    max_open_positions: int = Field(gt=0, le=10_000)
    max_daily_loss: float = Field(gt=0, le=1e12)
    max_drawdown_pct: float = Field(gt=0, le=100)
    max_trades_per_day: int = Field(gt=0, le=100_000)
    max_margin_usage_pct: float = Field(gt=0, le=100)
    max_spread_bps: float = Field(gt=0, le=10_000)
    max_slippage_points: int = Field(ge=0, le=100_000)
    max_quote_age_seconds: int = Field(gt=0, le=300)


class OrderSchema(Schema):
    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    volume: float = Field(gt=0, le=1e9)
    limit_price: float | None = Field(default=None, gt=0, le=1e15)
    stop_loss: float | None = Field(default=None, gt=0, le=1e15)
    take_profit: float | None = Field(default=None, gt=0, le=1e15)
    idempotency_key: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    position_id: str | None = Field(default=None, pattern=r"^[0-9]+$")

    def to_order(self) -> OrderInput:
        values = self.model_dump()
        values.pop("review_id", None)
        values.pop("confirmation", None)
        return OrderInput(**values)


class ReviewRequest(OrderSchema):
    pass


class SubmitRequest(OrderSchema):
    review_id: str = Field(min_length=8, max_length=100)
    confirmation: str = Field(min_length=1, max_length=100)


class ArmRequest(Schema):
    readiness_id: str = Field(min_length=8, max_length=100)
    phrase: str = Field(min_length=1, max_length=100)


class ActivateRequest(Schema):
    activation_id: str = Field(min_length=8, max_length=100)
    phrase: str = Field(min_length=1, max_length=100)


class PhraseRequest(Schema):
    phrase: str = Field(min_length=1, max_length=100)


class CancelRequest(Schema):
    confirmation: str = Field(min_length=1, max_length=100)


class PositionModifyRequest(Schema):
    position_id: str = Field(pattern=r"^[0-9]+$")
    stop_loss: float | None = Field(default=None, gt=0, le=1e15)
    take_profit: float | None = Field(default=None, gt=0, le=1e15)
    idempotency_key: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class PositionModifySubmit(PositionModifyRequest):
    review_id: str = Field(min_length=8, max_length=100)
    confirmation: str = Field(min_length=1, max_length=100)


def service(request: Request):
    value = getattr(request.app.state, "vantage", None)
    if value is None:
        raise HTTPException(503, "Vantage runtime is unavailable")
    return value


def paper_workspace(request: Request) -> dict:
    broker = request.app.state.broker
    simulator = request.app.state.simulator
    account = broker.get_balance()
    positions = broker.get_positions()
    return {
        "status": service(request).status(),
        "paper_session": simulator.status(),
        "account": {
            "currency": account.get("currency"), "balance": account.get("cash"),
            "cash": account.get("cash"), "equity": account.get("equity"),
            "margin": 0.0, "margin_free": account.get("cash"),
            "login_masked": "LOCAL PAPER", "server": "AXIOM local only",
        },
        "positions": positions,
        "orders": broker.get_orders(),
        "intents": [],
        "deals": [asdict(fill) for fill in broker.fills],
        "pnl": {
            "realized_visible_history": account.get("realized_pnl"),
            "unrealized": sum(float(item.get("unrealized_pnl") or 0) for item in positions),
            "note": "Local simulated paper accounting only.",
        },
        "simulated": True, "paper_only": True, "live_trading": False,
        "source": "AXIOM_LOCAL_PAPER",
    }


@router.get("/status")
def status(request: Request):
    return service(request).status()


@router.post("/session")
async def select_session(body: ModeRequest, request: Request):
    mode = SessionMode(body.mode)
    simulator = request.app.state.simulator
    # A paper engine cannot keep producing orders while a Vantage session is
    # selected. Stop it first, then switch the one execution session identity.
    if mode != SessionMode.PAPER:
        await simulator.stop()
    return service(request).select_mode(mode)


@router.get("/connection")
def connection(request: Request):
    svc = service(request)
    return {
        "status": svc.status(),
        "profiles": {
            mode.value: svc.store.credential_metadata(mode.value)
            for mode in (SessionMode.DEMO, SessionMode.LIVE)
        },
        "secret_store": getattr(request.app.state, "vantage_secret_status", "unavailable"),
        "mt5_package_available": mt5_package_available(),
        "mt5_integration": "official MetaTrader5 Python package + local MT5 terminal",
        "servers": ["VantageMarkets-Live 13"],
        "test_is_read_only": True,
        "note": "Test connection initializes/identifies/discovers/reconciles only. It never places, modifies or cancels an order.",
    }


@router.put("/credentials")
def save_credentials(body: CredentialRequest, request: Request):
    values = body.model_dump()
    values["password"] = body.password.get_secret_value()
    return service(request).save_credentials(
        SessionMode(body.environment), values, body.replace_existing
    )


@router.delete("/credentials/{environment}")
def delete_credentials(environment: str, request: Request):
    try:
        mode = SessionMode(environment)
    except ValueError as exc:
        raise HTTPException(422, "Unknown credential environment") from exc
    return service(request).delete_credentials(mode)


@router.post("/connection/test")
def test_connection(request: Request):
    return service(request).test_connection()


@router.post("/connection/disconnect")
def disconnect(request: Request):
    return service(request).disconnect()


@router.post("/reconcile")
def reconcile(request: Request):
    return service(request).reconcile(reason="operator requested reconciliation")


@router.get("/instruments")
def instruments(
    request: Request, q: str = Query(default="", max_length=100),
    product_type: str = Query(default="", max_length=40), selected_only: bool = False,
):
    return service(request).instruments(q, product_type, selected_only)


@router.put("/instruments/selection")
def select_instruments(body: InstrumentRequest, request: Request):
    return service(request).select_instruments(body.symbols)


@router.get("/quotes")
def quotes(request: Request):
    return service(request).quotes()


@router.get("/chart")
def chart(
    request: Request, symbol: str = Query(min_length=1, max_length=64),
    timeframe: str = "1m", limit: int = Query(default=300, ge=30, le=2000),
):
    return service(request).chart(symbol, timeframe, limit)


@router.put("/risk")
def save_risk(body: RiskRequest, request: Request):
    return service(request).save_risk_limits(body.model_dump())


@router.get("/readiness")
def readiness(request: Request):
    return service(request).readiness()


@router.post("/arm")
def arm(body: ArmRequest, request: Request):
    return service(request).arm(body.readiness_id, body.phrase)


@router.post("/activate")
def activate(body: ActivateRequest, request: Request):
    return service(request).activate(body.activation_id, body.phrase)


@router.post("/pause")
def pause(request: Request):
    return service(request).pause()


@router.post("/stop")
def stop(request: Request):
    return service(request).stop()


@router.post("/emergency")
async def emergency(request: Request):
    # One operator control covers the mutually exclusive execution session.
    # Stopping local paper also cancels its working orders; Vantage Stop
    # performs its own broker-truthful managed-order cancellation/reconcile.
    await request.app.state.simulator.stop()
    return service(request).emergency("operator API emergency stop")


@router.post("/emergency/clear")
def clear_emergency(body: PhraseRequest, request: Request):
    return service(request).clear_emergency(body.phrase)


@router.get("/overview")
def overview(request: Request):
    svc = service(request)
    return paper_workspace(request) if svc.mode == SessionMode.PAPER else svc.workspace()


@router.get("/workspace")
def workspace(request: Request):
    svc = service(request)
    return paper_workspace(request) if svc.mode == SessionMode.PAPER else svc.workspace()


@router.get("/activity")
def activity(request: Request, limit: int = Query(default=200, ge=1, le=1000)):
    svc = service(request)
    result = svc.activity(limit)
    if svc.mode == SessionMode.PAPER:
        # Manual/simulated paper events historically live in Repository rather
        # than the additive Vantage store.  Merge both redacted sources so the
        # operator does not see an empty audit trail after a real paper order.
        legacy = [
            {
                "timestamp": event.get("ts"), "environment": SessionMode.PAPER.value,
                "event_type": event.get("kind"), "payload": event.get("payload") or {},
                "id": event.get("id"),
            }
            for event in request.app.state.repository.list_system_events(limit)
        ]
        result["events"] = sorted(
            [*result["events"], *legacy],
            key=lambda event: str(event.get("timestamp") or ""), reverse=True,
        )[:limit]
    return result


@router.post("/orders/review")
def review_order(body: ReviewRequest, request: Request):
    return service(request).review(body.to_order())


@router.post("/orders/submit")
def submit_order(body: SubmitRequest, request: Request):
    return service(request).submit(body.to_order(), body.review_id, body.confirmation)


@router.post("/orders/{intent_id}/cancel")
def cancel_order(intent_id: str, body: CancelRequest, request: Request):
    return service(request).cancel(intent_id, body.confirmation)


@router.post("/positions/modify/review")
def review_position_modification(body: PositionModifyRequest, request: Request):
    values = body.model_dump()
    return service(request).review_position_modification(**values)


@router.post("/positions/modify/submit")
def submit_position_modification(body: PositionModifySubmit, request: Request):
    values = body.model_dump()
    return service(request).submit_position_modification(**values)
