"""AI & Model Lab HTTP routes — strictly PAPER / RESEARCH / ADVISORY ONLY.

Exposes:
- Kronos provider status, forecast-only Load/Unload & Enable/Disable toggles,
  cadence/timeout configuration, and validated bar forecasting (NSE & NASDAQ).
- Historical Dataset Manager (list, CSV/JSON import, read-only provider bar capture).
- Chronological Research & Walk-Forward Runner (explicit operator start, progress,
  cancellation, baseline & 2x cost-stress comparison).
- Model Registry (manual paper-reference promotion, rejection, SHA-256 export/import).
- Read-only research qualification: published rules, per-run verdicts, records.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.model_lab.kronos_provider import (
    KronosForecastProvider,
    KronosInputError,
    KronosUnavailableError,
)
from app.model_lab.registry import ModelLabService

router = APIRouter(prefix="/api/model-lab", tags=["model-lab"])


class KronosToggleBody(BaseModel):
    enabled: bool


class KronosConfigBody(BaseModel):
    cadence_seconds: int | None = Field(default=None, ge=15, le=86400)
    timeout_seconds: float | None = Field(default=None, ge=1.0, le=300.0)
    horizon: int | None = Field(default=None, ge=1, le=60)
    samples: int | None = Field(default=None, ge=4, le=32)
    min_context_bars: int | None = Field(default=None, ge=32, le=512)
    max_context_bars: int | None = Field(default=None, ge=32, le=512)
    max_forecast_age_seconds: int | None = Field(default=None, ge=30, le=86400)
    cpu_threads_cap: int | None = Field(default=None, ge=1, le=16)
    paper_session_enabled: bool | None = None
    weight_cap: float | None = Field(default=None, ge=0.0, le=0.25)
    min_confidence: float | None = Field(default=None, ge=0.50, le=0.95)
    max_uncertainty_pct: float | None = Field(default=None, ge=0.5, le=50.0)
    conflict_policy: str | None = Field(default=None, max_length=48)
    failure_policy: str | None = Field(default=None, max_length=48)


class KronosPaperPolicyBody(BaseModel):
    paper_session_enabled: bool | None = None
    weight_cap: float | None = Field(default=None, ge=0.0, le=0.25)
    min_confidence: float | None = Field(default=None, ge=0.50, le=0.95)
    max_uncertainty_pct: float | None = Field(default=None, ge=0.5, le=50.0)
    conflict_policy: str | None = Field(default=None, max_length=48)
    failure_policy: str | None = Field(default=None, max_length=48)


class KronosForecastBody(BaseModel):
    market: str = Field(default="NSE", pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(default="RELIANCE", min_length=1, max_length=64)
    timeframe: str = Field(default="1h", max_length=8)
    dataset_id: str | None = Field(default=None, max_length=120)
    bars: list[dict[str, Any]] | None = None
    horizon: int | None = Field(default=None, ge=1, le=60)
    samples: int | None = Field(default=None, ge=4, le=32)


class DatasetImportBody(BaseModel):
    dataset_id: str | None = Field(default=None, max_length=100)
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(default="1h", max_length=8)
    csv_text: str | None = None
    bars: list[dict[str, Any]] | None = None
    source: str = Field(default="Operator Local Import", max_length=160)
    feed: str = Field(default="LOCAL_OHLCV", max_length=80)
    corporate_action_adjustment: str = Field(default="UNVERIFIED_RAW", max_length=80)
    corporate_actions: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional axiom.corporate-actions payload (splits + cash dividends) with "
            "per-record source/version/provenance. Invalid, duplicate, conflicting or "
            "ambiguous records reject the whole import."
        ),
    )
    corporate_action_policy: str = Field(
        default="RAW",
        pattern="^(RAW|SPLIT_ADJUSTED|TOTAL_RETURN_ADJUSTED)$",
        description=(
            "Research convention for the derived series. Stored bars always stay RAW "
            "so paper fills never use adjusted prices."
        ),
    )
    survivorship_aware: bool = False
    real_market_data: bool = True


class CaptureLiveDatasetBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(default="1h", max_length=8)


class StartResearchBody(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=120)
    market: str | None = Field(default=None, max_length=12)
    timeframe: str | None = Field(default=None, max_length=8)
    train_frac: float = Field(default=0.60, ge=0.40, le=0.75)
    val_frac: float = Field(default=0.20, ge=0.10, le=0.30)
    embargo_bars: int = Field(default=5, ge=1, le=50)
    spread_bps: float = Field(default=4.0, ge=0.0, le=100.0)
    slippage_bps: float = Field(default=5.0, ge=0.0, le=100.0)
    stress_multiplier: float = Field(default=2.0, ge=1.25, le=5.0)
    min_trades: int = Field(default=5, ge=1, le=100)
    include_kronos: bool = False
    async_execution: bool = True


class ModelActionBody(BaseModel):
    note: str = Field(default="", max_length=240)


class ModelImportBody(BaseModel):
    bundle: dict[str, Any]


def _service(request: Request) -> ModelLabService:
    svc = getattr(request.app.state, "model_lab", None)
    if svc is None:
        raise HTTPException(503, "Model Lab service is unavailable")
    return svc


def _kronos(request: Request) -> KronosForecastProvider:
    prov = getattr(request.app.state, "kronos_provider", None)
    if prov is None:
        raise HTTPException(503, "Kronos forecast provider is unavailable")
    return prov


@router.get("/overview")
def model_lab_overview(request: Request):
    return _service(request).overview()


@router.get("/kronos/status")
def kronos_status(request: Request):
    return _kronos(request).status()


@router.post("/kronos/toggle")
def kronos_toggle(body: KronosToggleBody, request: Request):
    return _kronos(request).set_inference_enabled(body.enabled)


@router.post("/kronos/config")
def kronos_config(body: KronosConfigBody, request: Request):
    updates = body.model_dump(exclude_none=True)
    try:
        return _kronos(request).update_config(**updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/kronos/paper-policy")
def kronos_paper_policy_get(request: Request):
    return _kronos(request).paper_policy_view()


@router.post("/kronos/paper-policy")
def kronos_paper_policy_update(body: KronosPaperPolicyBody, request: Request):
    updates = body.model_dump(exclude_none=True)
    try:
        st = _kronos(request).update_config(**updates)
        return {"ok": True, "paper_policy": st["paper_policy"], "kronos": st}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/kronos/events")
def kronos_lifecycle_events(request: Request, limit: int = 200):
    return {"events": _kronos(request).lifecycle_events(limit=limit)}


@router.post("/kronos/load")
def kronos_load(request: Request):
    try:
        return _kronos(request).load_model()
    except KronosUnavailableError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/kronos/unload")
def kronos_unload(request: Request):
    return _kronos(request).unload_model()


@router.post("/kronos/clear-error")
def kronos_clear_error(request: Request):
    return _kronos(request).clear_error()


@router.post("/kronos/forecast")
def kronos_forecast(body: KronosForecastBody, request: Request):
    svc = _service(request)
    prov = _kronos(request)
    mkt = body.market.upper()

    bars_input: Any = body.bars
    sym = body.symbol.strip().upper()
    tf = body.timeframe.strip()

    if bars_input is None and body.dataset_id:
        try:
            meta, ds_bars = svc.datasets.load_dataset(body.dataset_id)
            bars_input = ds_bars
            mkt = str(meta.get("market") or mkt).upper()
            sym = str(meta.get("symbol") or sym).upper()
            tf = str(meta.get("timeframe") or tf)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    if bars_input is None:
        fetcher = (
            getattr(request.app.state, "nse_bars_provider", None)
            if mkt == "NSE"
            else getattr(request.app.state, "nasdaq_bars_provider", None)
        )
        if fetcher is None:
            raise HTTPException(409, f"No live read-only bar provider configured for {mkt}; select a dataset or configure Connections.")
        try:
            bars_input = fetcher(sym, tf)
        except Exception as exc:
            raise HTTPException(
                409,
                f"Cannot run real {mkt} Kronos forecast: read-only bars unavailable ({str(exc)[:180]}). Mock fallback is disabled.",
            ) from exc
        if not bars_input:
            raise HTTPException(
                409,
                f"UNAVAILABLE: 0 completed {mkt} bars returned for {sym} ({tf}). Configure read-only {mkt} market data or choose a verified dataset.",
            )

    try:
        return prov.predict(
            market=mkt,
            symbol=sym,
            timeframe=tf,
            bars=bars_input,
            horizon=body.horizon,
            samples=body.samples,
        )
    except KronosInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KronosUnavailableError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/datasets")
def list_datasets(request: Request):
    return {"datasets": _service(request).datasets.list_datasets()}


@router.post("/datasets/import")
def import_dataset(body: DatasetImportBody, request: Request):
    payload: Any = body.csv_text if body.csv_text else body.bars
    if not payload:
        raise HTTPException(422, "Provide either csv_text or bars list to import a dataset")
    try:
        meta = _service(request).datasets.register_dataset(
            dataset_id=body.dataset_id,
            market=body.market,
            symbol=body.symbol,
            timeframe=body.timeframe,
            bars_or_csv=payload,
            source=body.source,
            feed=body.feed,
            corporate_action_adjustment=body.corporate_action_adjustment,
            survivorship_aware=body.survivorship_aware,
            real_market_data=body.real_market_data,
            corporate_actions=body.corporate_actions,
            corporate_action_policy=body.corporate_action_policy,
        )
        return {"ok": True, "dataset": meta}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/datasets/capture-live")
def capture_live_dataset(body: CaptureLiveDatasetBody, request: Request):
    mkt = body.market.upper()
    sym = body.symbol.strip().upper()
    tf = body.timeframe.strip()
    fetcher = (
        getattr(request.app.state, "nse_bars_provider", None)
        if mkt == "NSE"
        else getattr(request.app.state, "nasdaq_bars_provider", None)
    )
    if fetcher is None:
        raise HTTPException(409, f"Read-only {mkt} historical bar provider is not available")
    try:
        bars = fetcher(sym, tf)
    except Exception as exc:
        raise HTTPException(409, f"Read-only {mkt} historical fetch failed: {str(exc)[:200]}") from exc
    if not bars or len(bars) < 60:
        raise HTTPException(
            409,
            f"Need >= 60 real completed {mkt} bars to register a research dataset (received {len(bars or [])}). Verify Connections first.",
        )
    try:
        meta = _service(request).datasets.register_dataset(
            market=mkt,
            symbol=sym,
            timeframe=tf,
            bars_or_csv=bars,
            source=f"{'Upstox Historical v3' if mkt == 'NSE' else 'Alpaca Market Data v2'} (Read-Only Capture)",
            feed="UPSTOX_V3" if mkt == "NSE" else "ALPACA_IEX_OR_SIP",
            corporate_action_adjustment="PROVIDER_DEFAULT_UNVERIFIED",
            survivorship_aware=False,
            real_market_data=True,
        )
        return {"ok": True, "dataset": meta}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/qualification/rules")
def qualification_rules(request: Request):
    """The published qualification rules (read-only; no evaluation)."""
    return _service(request).qualification_rules()


@router.get("/runs")
def list_runs(request: Request):
    return {"runs": _service(request).list_runs(limit=30)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    try:
        return _service(request).get_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/runs")
def start_run(body: StartResearchBody, request: Request):
    try:
        return _service(request).start_research_run(
            dataset_id=body.dataset_id,
            market=body.market,
            timeframe=body.timeframe,
            train_frac=body.train_frac,
            val_frac=body.val_frac,
            embargo_bars=body.embargo_bars,
            spread_bps=body.spread_bps,
            slippage_bps=body.slippage_bps,
            stress_multiplier=body.stress_multiplier,
            min_trades=body.min_trades,
            include_kronos=body.include_kronos,
            async_execution=body.async_execution,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/runs/{run_id}/qualification")
def get_run_qualification(run_id: str, request: Request):
    """Full immutable qualification record for one completed research run."""
    svc = _service(request)
    try:
        svc.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        return svc.get_qualification(run_id)
    except KeyError as exc:
        raise HTTPException(404, f"Run {run_id} has no qualification record (not completed yet?)") from exc


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request):
    try:
        return _service(request).cancel_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/models")
def list_models(request: Request, market: str = ""):
    return {"models": _service(request).list_models(market=market)}


@router.post("/models/{model_id}/promote")
def promote_model(model_id: str, body: ModelActionBody, request: Request):
    try:
        return _service(request).promote_model(model_id, note=body.note)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/models/{model_id}/reject")
def reject_model(model_id: str, body: ModelActionBody, request: Request):
    try:
        return _service(request).reject_model(model_id, reason=body.note or "Rejected by operator")
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/models/{model_id}/export")
def export_model(model_id: str, request: Request):
    try:
        return _service(request).export_model_bundle(model_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/models/import")
def import_model(body: ModelImportBody, request: Request):
    try:
        return {"ok": True, "model": _service(request).import_model_bundle(body.bundle)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
