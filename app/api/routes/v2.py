"""V2.1 API for the desktop application (additive; same bearer auth as V1).

Aggregates repository-recorded data for the dashboard pages. The UI can only
read state and request actions already available through the core — it can
never bypass the hard risk layer (emergency stop goes through the same
persisted flag; no endpoint can place orders or change risk limits).
"""

from __future__ import annotations

import json

from app import __version__
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.reporting.activity import explain_all
from app.reporting.weekly import WeeklyReport

router = APIRouter(prefix="/api/v2")


def _repo(request: Request):
    return request.app.state.repository


@router.get("/overview")
async def overview(request: Request):
    repository = _repo(request)
    settings = request.app.state.settings
    equity_state = repository.get_engine_state("sandbox_equity") or {}
    snapshots = repository.list_daily_snapshots(limit=2)
    return {
        "version": __version__,
        "api_latency_avg_ms": None if not getattr(request.app.state, "api_latencies", []) else round(
            sum(request.app.state.api_latencies) / len(request.app.state.api_latencies), 2),
        "api_requests_observed": len(getattr(request.app.state, "api_latencies", [])),
        "execution_mode": settings.broker.upper(),
        "live_trading": False,
        "mode_display": f"{settings.broker.upper()} · LIVE DISABLED",
        "emergency_stop": bool((repository.get_engine_state("emergency_stop") or {}).get("stopped")),
        "equity": equity_state.get("equity"),
        "drawdown": equity_state.get("drawdown"),
        "capital_initial": settings.initial_capital,
        "latest_snapshot": snapshots[0] if snapshots else None,
        "activity": explain_all(repository.list_system_events(limit=30)),
    }


@router.get("/trades")
async def trades(request: Request):
    repository = _repo(request)
    rows = []
    for fill in [e for e in repository.list_system_events(limit=500)
                 if e["kind"] == "SANDBOX_FILL_RECONCILED"]:
        payload = fill["payload"] if isinstance(fill["payload"], dict) else json.loads(fill["payload"])
        rows.append({"ts": fill["ts"], **payload})
    return {"fills": rows[:100], "sandbox_orders": repository.list_sandbox_orders(limit=50)}


@router.get("/performance")
async def performance(request: Request):
    repository = _repo(request)
    equity_state = repository.get_engine_state("sandbox_equity") or {}
    snapshots = repository.list_daily_snapshots(limit=400)
    equity_curve = [{"date": row["date"], "equity": row["equity"]}
                    for row in reversed(snapshots) if row["equity"] is not None]
    initial = request.app.state.settings.initial_capital
    equity = equity_state.get("equity") or (equity_curve[-1]["equity"] if equity_curve else initial)
    total_return = (equity / initial - 1) if initial and equity is not None else None
    peak = max([row["equity"] for row in equity_curve], default=initial)
    drawdown = (1 - equity / peak) if peak and equity is not None else None
    return {
        "equity_curve": equity_curve,
        "realized": sum(float(r["realized"] or 0) for r in snapshots),
        "unrealized": float(snapshots[-1]["unrealized"] or 0) if snapshots else 0.0,
        "total_return_pct": None if total_return is None else round(total_return * 100, 4),
        "drawdown": drawdown,
        "labels": {"equity": "CALCULATED", "realized": "CALCULATED",
                   "unrealized": "CALCULATED"},
    }


@router.get("/strategies")
async def strategies(request: Request):
    repository = _repo(request)
    return {
        "promoted_versions": [row for row in repository.list_model_versions(limit=10)
                              if row["promoted"]],
        "candidates": repository.list_candidates(limit=25),
        "signals": repository.list_signals(limit=25),
    }


@router.get("/learning")
async def learning(request: Request):
    repository = _repo(request)
    return {
        "model_versions": repository.list_model_versions(limit=10),
        "learning_events": repository.list_learning_events(limit=30),
        "shadow_trades": repository.list_shadow_trades(limit=50),
        "shadow_performance": repository.get_engine_state("shadow_performance") or {},
    }


@router.get("/experiments")
async def experiments(request: Request):
    repository = _repo(request)
    return {"experiments": repository.list_experiments(limit=20),
            "candidates": repository.list_candidates(limit=50)}


@router.get("/market_data")
async def market_data(request: Request):
    repository = _repo(request)
    return {"universe": repository.instrument_counts(),
            "universe_syncs": repository.list_universe_syncs(limit=3),
            "coverage": repository.list_coverage(limit=100),
            "quality_events": repository.list_quality_events(limit=30),
            "download_jobs": repository.list_download_jobs(limit=20)}


@router.get("/risk")
async def risk(request: Request):
    repository = _repo(request)
    settings = request.app.state.settings
    equity_state = repository.get_engine_state("sandbox_equity") or {}
    risk_events = [e for e in repository.list_system_events(limit=200)
                   if e["kind"] in {"RISK_REJECT", "EMERGENCY_STOP", "EMERGENCY_STOP_REQUESTED"}]
    limits = {
        "max_position_value": settings.max_position_value,
        "max_open_positions": settings.max_open_positions,
        "max_daily_loss": settings.max_daily_loss,
        "max_trades_per_day": settings.max_trades_per_day,
    }
    return {"limits": limits, "limits_note": "hard risk layer; learning cannot change these",
            "equity": equity_state.get("equity"), "drawdown": equity_state.get("drawdown"),
            "emergency_stop": bool((repository.get_engine_state("emergency_stop") or {}).get("stopped")),
            "risk_events": len(risk_events)}


@router.get("/logs")
async def logs(request: Request):
    repository = _repo(request)
    return {"raw_events": repository.list_system_events(limit=100),
            "activity": explain_all(repository.list_system_events(limit=100))}


@router.get("/report/weekly")
async def report_weekly(request: Request, format: str = "markdown"):
    repository = _repo(request)
    report = WeeklyReport(repository, settings=request.app.state.settings)
    if format == "pdf":
        return Response(content=report.pdf(), media_type="application/pdf")
    return Response(content=report.markdown(), media_type="text/markdown")


# ------------------------------------------------------------- FINAL API ----

@router.get("/command_center")
async def command_center(request: Request, q: str = ""):
    from app.research.command_center import CommandCenter

    repository = _repo(request)
    return CommandCenter(repository).ask(q)


@router.get("/portfolio")
async def portfolio(request: Request):
    from app.learning.portfolio import PortfolioIntelligence

    repository = _repo(request)
    engine = getattr(request.app.state, "sandbox_engine", None)
    broker = getattr(request.app.state, "sandbox_broker", None)
    intelligence = PortfolioIntelligence(repository)
    if broker is not None:
        snapshot = intelligence.snapshot(
            broker.ledger,
            open_positions=[{"quantity": broker.ledger.position_quantity,
                             "price": broker.ledger.mark
                             or broker.ledger.position_avg_price}])
    else:
        equity_state = repository.get_engine_state("sandbox_equity") or {}
        snapshot = {"equity": equity_state.get("equity"),
                    "exposure_ratio": None,
                    "note": "no active sandbox broker; recorded state only"}
    allocation = repository.get_engine_state("capital_allocation") or {}
    return {"snapshot": snapshot, "allocation": allocation,
            "hard_cap": request.app.state.settings.max_strategy_allocation}


@router.get("/attribution")
async def attribution(request: Request):
    from app.research.attribution import Attribution

    return Attribution(_repo(request)).attribute()


@router.get("/research_lab")
async def research_lab(request: Request):
    repository = _repo(request)
    return {"experiments": repository.list_experiments(limit=20),
            "candidates": repository.list_candidates(limit=50),
            "questions": repository.list_research_questions(limit=30),
            "lineage": repository.list_lineage(limit=50),
            "lifecycle": repository.lifecycle_history(limit=50)}


@router.get("/learning_detail")
async def learning_detail(request: Request):
    from app.learning.calibration import ConfidenceCalibration

    repository = _repo(request)
    calibration = ConfidenceCalibration(repository)
    strategies = {row["strategy"] for row in repository.list_calibration(limit=200)}
    return {"calibration": {name: calibration.reliability(name) for name in strategies},
            "regime_history_count": len(repository.list_regime_history_all(limit=2000)),
            "shadow_performance": repository.get_engine_state("shadow_performance") or {}}


@router.get("/market_intelligence")
async def market_intelligence(request: Request):
    repository = _repo(request)
    return {"universe": repository.instrument_counts(),
            "coverage": repository.list_coverage(limit=100),
            "quality": repository.list_quality_events(limit=30),
            "regimes": repository.list_regime_history_all(limit=50),
            "regime_similarity_disclaimer":
                "similarity to past regimes is observation, not a guarantee"}


def _equity_points(repository) -> list[float]:
    return [float(row["equity"])
            for row in reversed(repository.list_daily_snapshots(limit=400))
            if row["equity"] is not None]


@router.get("/charts/equity.svg")
async def chart_equity(request: Request):
    from app.reporting.charts import line_chart_svg

    equities = _equity_points(_repo(request))
    points = list(enumerate(equities))  # x = index (dates are labels, not numbers)
    svg = line_chart_svg("Equity (CALCULATED from recorded snapshots)", points)
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/charts/drawdown.svg")
async def chart_drawdown(request: Request):
    from app.reporting.charts import line_chart_svg

    equities = _equity_points(_repo(request))
    peak = None
    dd_points = []
    for index, equity in enumerate(equities):
        peak = equity if peak is None else max(peak, equity)
        dd_points.append((index, 1 - equity / peak if peak else 0.0))
    svg = line_chart_svg("Drawdown (CALCULATED)", dd_points, color="#ff9aa5")
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/charts/attribution.svg")
async def chart_attribution(request: Request):
    from app.research.attribution import Attribution
    from app.reporting.charts import bar_chart_svg

    result = Attribution(_repo(request)).attribute()
    values = [(name, round(stats.get("net_pnl", 0.0), 2))
              for name, stats in (result["dimensions"].get("strategy") or {}).items()]
    svg = bar_chart_svg("NET P&L by strategy (CALCULATED)", values)
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/report")
async def report(request: Request, period: str = "weekly",
                 format: str = "markdown"):
    from app.reporting.weekly import PeriodReport

    days = {"daily": 1, "weekly": 7, "monthly": 30}.get(period, 7)
    repository = _repo(request)
    report = PeriodReport(repository, settings=request.app.state.settings,
                          days=days, title=f"Arena {period.capitalize()} Report")
    if format == "pdf":
        return Response(content=report.pdf(), media_type="application/pdf")
    return Response(content=report.markdown(), media_type="text/markdown")
