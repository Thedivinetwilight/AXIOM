from fastapi import APIRouter, HTTPException, Query, Request
from app.api.schemas import BacktestRequest
from app.backtesting.costs import ExecutionConfig
from app.backtesting.engine import BacktestEngine
from app.api.services import load_dataset

router = APIRouter()


@router.get("/backtests")
def backtests(
    request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)
):
    return {"backtests": request.app.state.repository.list_backtests(limit, offset)}


@router.post("/backtests/run")
def run_backtest(body: BacktestRequest, request: Request):
    lock = request.app.state.research_lock
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "A research job is already running")
    try:
        strategy = body.build_strategy()
        bars = load_dataset(request.app, body)
        config = ExecutionConfig(
            **{name: getattr(body, name) for name in ExecutionConfig.__dataclass_fields__}
        )
        result = BacktestEngine(config, annualization=body.annualization).run(
            bars, strategy, body.timeframe
        )
        request.app.state.repository.save_backtest(result)
        return {
            "id": result.id,
            "label": result.label,
            "metrics": result.metrics,
            "warnings": result.warnings,
        }
    finally:
        lock.release()


@router.get("/backtests/{backtest_id}")
def get_backtest(backtest_id: str, request: Request):
    result = request.app.state.repository.get_backtest(backtest_id)
    if result is None:
        raise HTTPException(404, "Backtest not found")
    return result


@router.get("/tournaments")
def tournaments(request: Request, limit: int = Query(20, ge=1, le=100)):
    return {"research_runs": request.app.state.repository.list_research(limit)}
