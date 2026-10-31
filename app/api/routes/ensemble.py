"""Authenticated FastAPI routes for the Parallel Strategy Ensemble & Portfolio Risk Allocator (`/api/ensemble/*`).

Strictly PAPER / RESEARCH / ADVISORY:
- Never arms, activates, or calls any live broker order client.
- `execute_in_paper=True` only submits local simulated orders into `LiveMarketPaperEngine`
  when the market's paper session is already `RUNNING` (`0` broker order API calls).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.auth import authenticate

router = APIRouter(
    prefix="/api/ensemble",
    tags=["ensemble"],
    dependencies=[Depends(authenticate)],
)


def _ens(request: Request):
    svc = getattr(request.app.state, "ensemble_service", None)
    if svc is None:
        raise HTTPException(503, "StrategyEnsembleService is unavailable")
    return svc


class EnsembleConfigBody(BaseModel):
    preset: str | None = Field(default=None, max_length=32)
    enabled_families: list[str] | None = None
    custom_variants: list[dict[str, Any]] | None = None
    concurrency_limit: int | None = Field(default=None, ge=1, le=32)
    batch_timeout_seconds: float | None = Field(default=None, ge=1.0, le=120.0)
    default_timeframe: str | None = Field(default=None, max_length=8)
    market: str | None = Field(default=None, pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    allocator_updates: dict[str, Any] | None = None
    manual_weights: dict[str, float] | None = None


class EvaluateWatchlistBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbols: list[str] | None = None
    timeframe: str | None = Field(default=None, max_length=8)
    execute_in_paper: bool = False


class AblationBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    dataset_id: str = Field(min_length=1, max_length=128)
    timeframe: str = Field(default="1h", max_length=8)
    embargo_bars: int = Field(default=5, ge=1, le=60)
    evaluate_kronos_if_ready: bool = True
    persist_learned_weights: bool = True


class BenchmarkBody(BaseModel):
    variant_counts: list[int] | None = None
    instrument_counts: list[int] | None = None
    concurrency_limit: int | None = Field(default=None, ge=1, le=32)


@router.get("/overview")
def ensemble_overview(request: Request):
    return _ens(request).overview()


@router.get("/variants")
def list_variants(
    request: Request,
    family: str = Query(default=""),
    enabled_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
):
    svc = _ens(request)
    items = svc.registry.list_variants(enabled_only=enabled_only)
    fam = family.strip().lower()
    if fam:
        items = [v for v in items if v.family == fam]
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    cur = min(page, total_pages)
    start = (cur - 1) * page_size
    return {
        "page": cur,
        "page_size": page_size,
        "total_variants": total,
        "total_pages": total_pages,
        "items": [v.to_dict() for v in items[start : start + page_size]],
    }


@router.post("/config")
def update_ensemble_config(body: EnsembleConfigBody, request: Request):
    try:
        return _ens(request).update_config(
            preset=body.preset,
            enabled_families=body.enabled_families,
            custom_variants=body.custom_variants,
            concurrency_limit=body.concurrency_limit,
            batch_timeout_seconds=body.batch_timeout_seconds,
            default_timeframe=body.default_timeframe,
            market=body.market,
            allocator_updates=body.allocator_updates,
            manual_weights=body.manual_weights,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/evaluate")
def evaluate_watchlist(body: EvaluateWatchlistBody, request: Request):
    try:
        return _ens(request).evaluate_watchlist_cycle(
            body.market,
            symbols=body.symbols,
            timeframe=body.timeframe,
            execute_in_paper=body.execute_in_paper,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/ablation")
def run_ablation(body: AblationBody, request: Request):
    try:
        return _ens(request).run_ablation_study(
            market=body.market,
            dataset_id=body.dataset_id,
            timeframe=body.timeframe,
            embargo_bars=body.embargo_bars,
            evaluate_kronos_if_ready=body.evaluate_kronos_if_ready,
            persist_learned_weights=body.persist_learned_weights,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/benchmark")
def run_benchmark(body: BenchmarkBody, request: Request):
    try:
        return _ens(request).run_scaling_benchmark(
            variant_counts=body.variant_counts,
            instrument_counts=body.instrument_counts,
            concurrency_limit=body.concurrency_limit,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/decisions")
def get_decisions(
    request: Request,
    market: str = Query(default=""),
    symbol: str = Query(default=""),
    limit: int = Query(default=40, ge=1, le=200),
):
    return {
        "decisions": _ens(request).list_recent_decisions(
            market=market or None,
            symbol=symbol or None,
            limit=limit,
        )
    }
