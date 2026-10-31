"""Unified Multi-Market Terminal API (NSE INR + NASDAQ USD, Paper & Gated Live).

Powers the AXIOM multi-market operator desktop terminal across:
- NSE (Upstox read-only market data + BOD master; separate gated UPSTOX_LIVE)
- NASDAQ (Alpaca read-only market data + asset catalog; separate gated ALPACA_LIVE)
- Local ``LIVE_MARKET_PAPER`` simulation on real read-only feeds (zero broker orders)
- Global atomic Emergency Stop preserving confirmed position protection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.autonomy.config import AutonomySettings
from app.autonomy.upstox import InstrumentCatalog, ReconciliationFailure, UpstoxAuthError, UpstoxReadOnly
from app.autonomy.upstox_token import resolve_live_token, resolve_token
from app.data.alpaca import AlpacaHistoricalProvider, feed_label, validate_us_symbol
from app.data.models import TIMEFRAMES
from app.market_data.provider_verification import (
    build_provider_matrix,
    verify_alpaca_market_data,
    verify_upstox_market_data,
)
from app.markets.alpaca_credentials import resolve_alpaca_data_credentials
from app.markets.nse_reconciliation import nse_session_status
from app.markets.us_calendar import USEquitiesCalendar

router = APIRouter(prefix="/api/markets")


class WatchlistItemBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)


class PaperSessionStartBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    strategy: str = Field(default="manual_only", max_length=40)
    timeframe: str = Field(default="5m", max_length=8)
    symbols: list[str] = Field(default_factory=list)


class PaperSessionTransitionBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    action: str = Field(pattern="^(pause|resume|stop)$")
    reason: str = Field(default="", max_length=200)


class PaperResetBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    confirm: bool = False


class PaperConfigBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    starting_cash: float | None = Field(default=None, gt=0)
    max_order_value: float | None = Field(default=None, gt=0)
    max_symbol_exposure: float | None = Field(default=None, gt=0)
    max_total_exposure: float | None = Field(default=None, gt=0)
    max_open_positions: int | None = Field(default=None, gt=0)
    max_daily_loss: float | None = Field(default=None, gt=0)
    max_daily_orders: int | None = Field(default=None, gt=0)
    max_quote_age_seconds: float | None = Field(default=None, gt=0)
    spread_bps: float | None = Field(default=None, ge=0)
    slippage_bps: float | None = Field(default=None, ge=0)
    brokerage_bps: float | None = Field(default=None, ge=0)


class PaperOrderBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)
    side: str = Field(pattern="^(BUY|SELL|buy|sell)$")
    qty: float = Field(gt=0)
    order_type: str = Field(default="MARKET", pattern="^(MARKET|LIMIT|STOP|STOP_LIMIT|market|limit|stop|stop_limit)$")
    product: str = Field(default="D", max_length=12)
    time_in_force: str = Field(default="DAY", pattern="^(DAY|GTC|IOC|day|gtc|ioc)$")
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    client_order_id: str | None = Field(default=None, max_length=64)
    allow_off_hours_working: bool = False


class PaperReplaceBody(BaseModel):
    qty: float | None = Field(default=None, gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)


class PaperProtectionBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)


class GlobalEmergencyBody(BaseModel):
    reason: str = Field(default="operator global emergency stop", max_length=200)


class GlobalResetBody(BaseModel):
    confirm_phrase: str = Field(default="", max_length=80)


def _paper_engine(request: Request):
    eng = getattr(request.app.state, "live_paper_engine", None)
    if eng is None:
        raise HTTPException(503, "Live-Market Paper engine is not initialized")
    return eng


def _nasdaq_catalog(request: Request):
    cat = getattr(request.app.state, "nasdaq_catalog", None)
    if cat is None:
        raise HTTPException(503, "NASDAQ catalog is not initialized")
    return cat


def _nse_search_catalog(request: Request) -> InstrumentCatalog:
    autonomy = getattr(request.app.state, "autonomy", None)
    if autonomy is not None and autonomy.catalog.instruments:
        return autonomy.catalog
    seed_cat = getattr(request.app.state, "nse_seed_catalog", None)
    if seed_cat is not None:
        return seed_cat
    fallback = InstrumentCatalog(AutonomySettings(), request.app.state.data_dir / "nse-seed-fallback.json")
    fallback.load_seed_fallback()
    request.app.state.nse_seed_catalog = fallback
    return fallback


def _upstox_readonly_token(request: Request) -> tuple[str, str]:
    tok, src = resolve_token(getattr(request.app.state, "upstox_token", None))
    if tok:
        return tok, src
    env_tok = str(getattr(request.app.state.settings, "market_data_access_token", "") or "").strip()
    if env_tok:
        return env_tok, "environment"
    return "", "none"


def _alpaca_readonly_creds(request: Request) -> tuple[dict[str, str], str]:
    return resolve_alpaca_data_credentials(
        getattr(request.app.state, "alpaca_data_store", None),
        request.app.state.settings,
    )


def _live_status_summary(request: Request) -> dict[str, Any]:
    autonomy = getattr(request.app.state, "autonomy", None)
    upstox_live_tok, upstox_live_src = resolve_live_token(getattr(request.app.state, "upstox_live_token", None))
    autonomy_sessions = autonomy.sessions() if autonomy is not None else []
    nse_live_flag = bool(getattr(getattr(autonomy, "settings", None), "live_enabled", False))
    nse_algo_name = bool(str(getattr(getattr(autonomy, "settings", None), "algo_name", "") or "").strip())

    broker_rt = getattr(request.app.state, "alpaca_broker_runtime", None)
    alpaca_svc = getattr(request.app.state, "alpaca_broker", None) or (broker_rt.service if broker_rt else None)
    alpaca_creds = broker_rt.credential_status() if broker_rt else None
    us_live_flag = bool(request.app.state.settings.alpaca_broker_enabled)
    us_live_creds = bool(alpaca_creds and alpaca_creds["live"]["present"])

    return {
        "default_live_execution": "OFF",
        "nse": {
            "market": "NSE",
            "currency": "INR",
            "broker": "Upstox (Protected Multi-Leg GTT v3)",
            "mode_id": "UPSTOX_LIVE",
            "live_master_flag": nse_live_flag,
            "live_credential_present": bool(upstox_live_tok),
            "live_credential_source": upstox_live_src,
            "algo_name_configured": nse_algo_name,
            "availability": (
                "AVAILABLE_BEHIND_GATE"
                if (nse_live_flag and upstox_live_tok and nse_algo_name)
                else "NOT_AVAILABLE"
            ),
            "missing_dependencies": [
                dep
                for ok, dep in (
                    (nse_live_flag, "Set AUTONOMY_LIVE_ENABLED=true in host environment"),
                    (bool(upstox_live_tok), "Save separate UPSTOX_LIVE_ACCESS_TOKEN in Connections"),
                    (nse_algo_name, "Set UPSTOX_ALGO_NAME and register SEBI static IP on Upstox"),
                )
                if not ok
            ],
            "autonomy_session": autonomy_sessions[0] if autonomy_sessions else None,
        },
        "nasdaq": {
            "market": "NASDAQ",
            "currency": "USD",
            "broker": "Alpaca Trading API v2",
            "mode_id": "ALPACA_LIVE",
            "live_master_flag": us_live_flag,
            "live_credential_present": us_live_creds,
            "live_credential_source": alpaca_creds["live"]["source"] if alpaca_creds else "none",
            "availability": (
                "AVAILABLE_BEHIND_GATE"
                if (us_live_flag and us_live_creds)
                else "NOT_AVAILABLE"
            ),
            "missing_dependencies": [
                dep
                for ok, dep in (
                    (us_live_flag, "Set ALPACA_BROKER_ENABLED=true in host environment"),
                    (us_live_creds, "Save separate ALPACA_BROKER_LIVE key & secret in Connections"),
                )
                if not ok
            ],
            "broker_status": alpaca_svc.status() if alpaca_svc is not None else None,
        },
    }


def _build_readiness_matrix(request: Request) -> dict[str, Any]:
    """Structured per-market / per-mode readiness checklists with honest owner-verification state."""
    eng = _paper_engine(request)
    nse_snap = eng.market_snapshot("NSE")
    us_snap = eng.market_snapshot("NASDAQ")
    live_sum = _live_status_summary(request)
    nse_cat = _nse_search_catalog(request)
    us_cat = _nasdaq_catalog(request)
    receipts = getattr(request.app.state, "owner_verification_receipts", None) or {}

    upstox_tok, upstox_src = _upstox_readonly_token(request)
    upstox_rec = receipts.get("UPSTOX_MARKET_DATA") if upstox_tok else None
    nse_estop = nse_snap["session_state"] == "EMERGENCY_STOPPED"
    nse_ex_sess = nse_snap["exchange_session"]
    nse_sess_open = bool(nse_ex_sess["is_open"])

    nse_paper_items = [
        {
            "id": "upstox_token",
            "label": "Upstox read-only access token stored",
            "state": "PASS" if bool(upstox_tok) else "BLOCKED",
            "detail": f"Configured via {upstox_src}" if upstox_tok else "Missing UPSTOX_ACCESS_TOKEN (save in 02 Connections)",
        },
        {
            "id": "upstox_owner_probe",
            "label": "Read-only NSE quote connection verified",
            "state": "PASS" if upstox_rec else "NOT_VERIFIED",
            "detail": (
                f"Verified ({upstox_rec.get('connection_state')}) at {upstox_rec.get('verified_at')}"
                if upstox_rec
                else "NOT VERIFIED with owner credentials in this session (click 'Verify NSE Quote' in 02 Connections)"
            ),
        },
        {
            "id": "nse_catalog",
            "label": "NSE instrument catalog & ISIN resolution",
            "state": "PASS" if nse_cat.source_kind == "authoritative_upstox_bod" else "INFO",
            "detail": f"source={nse_cat.source_kind} ({len(nse_cat.instruments)} instruments)",
        },
        {
            "id": "nse_session",
            "label": "NSE regular trading hours (09:15–15:30 IST)",
            "state": "PASS" if nse_sess_open else "INFO",
            "detail": f"Session={nse_ex_sess['state']} (MARKET fills require open session; off-hours LIMIT orders queue)",
        },
        {
            "id": "nse_risk_estop",
            "label": "Bounded INR paper risk limits & Emergency Stop clear",
            "state": "BLOCKED" if nse_estop else "PASS",
            "detail": "EMERGENCY_STOPPED — click Clear Halt first" if nse_estop else "INR paper limits active; 0 broker order calls",
        },
    ]
    nse_paper_blockers = [i["detail"] for i in nse_paper_items if i["state"] == "BLOCKED"]
    nse_paper_status = (
        "BLOCKED"
        if nse_paper_blockers
        else ("NOT_VERIFIED" if not upstox_rec else ("READY" if nse_sess_open else "MARKET_CLOSED_READY"))
    )

    alpaca_creds, alpaca_src = _alpaca_readonly_creds(request)
    has_alpaca_data = bool(alpaca_creds["api_key"] and alpaca_creds["api_secret"])
    alpaca_rec = receipts.get("ALPACA_MARKET_DATA") if has_alpaca_data else None
    us_estop = us_snap["session_state"] == "EMERGENCY_STOPPED"
    us_ex_sess = us_snap["exchange_session"]
    us_sess_open = bool(us_ex_sess["is_open"])

    us_paper_items = [
        {
            "id": "alpaca_data_keys",
            "label": "Alpaca Market Data API Key & Secret stored",
            "state": "PASS" if has_alpaca_data else "BLOCKED",
            "detail": f"Configured via {alpaca_src} (feed={alpaca_creds['feed'].upper()})" if has_alpaca_data else "Missing ALPACA_MARKET_DATA keys (save in 02 Connections)",
        },
        {
            "id": "alpaca_owner_probe",
            "label": "Read-only NASDAQ quote connection verified",
            "state": "PASS" if alpaca_rec else "NOT_VERIFIED",
            "detail": (
                f"Verified ({alpaca_rec.get('connection_state')}, feed={str(alpaca_rec.get('feed') or '').upper()}) at {alpaca_rec.get('verified_at')}"
                if alpaca_rec
                else "NOT VERIFIED with owner credentials in this session (click 'Verify AAPL Quote' in 02 Connections)"
            ),
        },
        {
            "id": "nasdaq_catalog",
            "label": "NASDAQ symbol catalog",
            "state": "PASS" if us_cat.source_kind == "authoritative_alpaca_assets" else "INFO",
            "detail": f"source={us_cat.source_kind} ({len(us_cat.instruments)} symbols)",
        },
        {
            "id": "nasdaq_session",
            "label": "NASDAQ regular session (09:30–16:00 ET)",
            "state": "PASS" if us_sess_open else "INFO",
            "detail": f"Session={us_ex_sess['state']} (MARKET fills require regular ET session)",
        },
        {
            "id": "nasdaq_risk_estop",
            "label": "Bounded USD paper risk limits & Emergency Stop clear",
            "state": "BLOCKED" if us_estop else "PASS",
            "detail": "EMERGENCY_STOPPED — click Clear Halt first" if us_estop else "USD paper limits active; 0 broker order calls",
        },
    ]
    us_paper_blockers = [i["detail"] for i in us_paper_items if i["state"] == "BLOCKED"]
    us_paper_status = (
        "BLOCKED"
        if us_paper_blockers
        else ("NOT_VERIFIED" if not alpaca_rec else ("READY" if us_sess_open else "MARKET_CLOSED_READY"))
    )

    nse_l = live_sum["nse"]
    nse_live_items = [
        {
            "id": "nse_live_flag",
            "label": "Host master gate AUTONOMY_LIVE_ENABLED=true",
            "state": "PASS" if nse_l["live_master_flag"] else "BLOCKED",
            "detail": "Enabled" if nse_l["live_master_flag"] else "Default OFF — requires AUTONOMY_LIVE_ENABLED=true",
        },
        {
            "id": "nse_live_token",
            "label": "Separate UPSTOX_LIVE_ACCESS_TOKEN slot",
            "state": "PASS" if nse_l["live_credential_present"] else "BLOCKED",
            "detail": f"Source={nse_l['live_credential_source']}" if nse_l["live_credential_present"] else "Separate live token not saved",
        },
        {
            "id": "nse_sebi_algo",
            "label": "SEBI static IPv4 & UPSTOX_ALGO_NAME registration",
            "state": "PASS" if nse_l["algo_name_configured"] else "BLOCKED",
            "detail": "UPSTOX_ALGO_NAME configured" if nse_l["algo_name_configured"] else "External owner/broker prerequisite: UPSTOX_ALGO_NAME + static IP whitelist",
        },
        {
            "id": "nse_live_bod",
            "label": "Same-day authoritative Upstox BOD catalog",
            "state": "PASS" if nse_cat.source_kind == "authoritative_upstox_bod" else "BLOCKED",
            "detail": f"source={nse_cat.source_kind}",
        },
    ]

    us_l = live_sum["nasdaq"]
    alpaca_b_status = us_l.get("broker_status") or {}
    alpaca_live_readiness = ((alpaca_b_status.get("live") or {}).get("last_readiness")) or {}
    us_live_acct_ok = bool(alpaca_live_readiness.get("ready"))
    us_live_items = [
        {
            "id": "us_live_flag",
            "label": "Host master gate ALPACA_BROKER_ENABLED=true",
            "state": "PASS" if us_l["live_master_flag"] else "BLOCKED",
            "detail": "Enabled" if us_l["live_master_flag"] else "Default OFF — requires ALPACA_BROKER_ENABLED=true",
        },
        {
            "id": "us_live_creds",
            "label": "Separate ALPACA_BROKER_LIVE API Key & Secret",
            "state": "PASS" if us_l["live_credential_present"] else "BLOCKED",
            "detail": f"Source={us_l['live_credential_source']}" if us_l["live_credential_present"] else "Separate live broker credentials not saved",
        },
        {
            "id": "us_live_preflight",
            "label": "Read-only live Alpaca account, clock & risk preflight",
            "state": "PASS" if us_live_acct_ok else "NOT_VERIFIED",
            "detail": "Live preflight passed" if us_live_acct_ok else "NOT VERIFIED on owner live Alpaca account (run Test Live Connection in 06 Risk & live gates)",
        },
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verification_note": "CI and mocked unit tests never count as owner connectivity. Items remain NOT_VERIFIED until tested with credentials entered in AXIOM's local UI.",
        "modes": {
            "NSE_PAPER": {
                "mode_key": "NSE_PAPER",
                "market": "NSE",
                "currency": "INR",
                "mode": "LIVE-MARKET PAPER",
                "overall_status": nse_paper_status,
                "owner_connection_verified": bool(upstox_rec),
                "items": nse_paper_items,
                "blockers": nse_paper_blockers,
                "next_action": (
                    "Open 02 Connections and save your daily Upstox read-only access token."
                    if not upstox_tok
                    else (
                        "Open 02 Connections and click 'Verify NSE Quote' to confirm live read-only NSE connectivity."
                        if not upstox_rec
                        else (
                            "Clear Emergency Stop in 06 Risk & live gates before placing paper orders."
                            if nse_estop
                            else "Ready for NSE LIVE-MARKET PAPER (simulated INR ledger, 0 broker order calls)."
                        )
                    )
                ),
            },
            "NASDAQ_PAPER": {
                "mode_key": "NASDAQ_PAPER",
                "market": "NASDAQ",
                "currency": "USD",
                "mode": "LIVE-MARKET PAPER",
                "overall_status": us_paper_status,
                "owner_connection_verified": bool(alpaca_rec),
                "items": us_paper_items,
                "blockers": us_paper_blockers,
                "next_action": (
                    "Open 02 Connections and save your Alpaca Market Data API Key & Secret (IEX or SIP)."
                    if not has_alpaca_data
                    else (
                        "Open 02 Connections and click 'Verify AAPL Quote' to confirm live read-only NASDAQ connectivity."
                        if not alpaca_rec
                        else (
                            "Clear Emergency Stop in 06 Risk & live gates before placing paper orders."
                            if us_estop
                            else "Ready for NASDAQ LIVE-MARKET PAPER (simulated USD ledger, 0 broker order calls)."
                        )
                    )
                ),
            },
            "NSE_LIVE": {
                "mode_key": "NSE_LIVE",
                "market": "NSE",
                "currency": "INR",
                "mode": "LIVE-MARKET LIVE",
                "overall_status": "AVAILABLE_BEHIND_GATE" if nse_l["availability"] == "AVAILABLE_BEHIND_GATE" else "DISABLED",
                "owner_connection_verified": False,
                "items": nse_live_items,
                "blockers": list(nse_l["missing_dependencies"]),
                "next_action": (
                    nse_l["missing_dependencies"][0]
                    if nse_l["missing_dependencies"]
                    else "Complete read-only preflight, Arm, and Activate on /autonomy when ready."
                ),
            },
            "NASDAQ_LIVE": {
                "mode_key": "NASDAQ_LIVE",
                "market": "NASDAQ",
                "currency": "USD",
                "mode": "LIVE-MARKET LIVE",
                "overall_status": (
                    "READY_TO_ARM"
                    if (us_l["availability"] == "AVAILABLE_BEHIND_GATE" and us_live_acct_ok)
                    else ("NOT_VERIFIED" if us_l["availability"] == "AVAILABLE_BEHIND_GATE" else "DISABLED")
                ),
                "owner_connection_verified": us_live_acct_ok,
                "items": us_live_items,
                "blockers": list(us_l["missing_dependencies"]),
                "next_action": (
                    us_l["missing_dependencies"][0]
                    if us_l["missing_dependencies"]
                    else "Configure bounded USD risk limits, run read-only live preflight, Arm (15-min), and type confirmation phrase in 06 Risk & live gates."
                ),
            },
        },
    }


@router.get("/readiness")
def markets_readiness(request: Request):
    return _build_readiness_matrix(request)


@router.get("/overview")
def markets_overview(request: Request):
    eng = _paper_engine(request)
    nse_cat = _nse_search_catalog(request)
    nasdaq_cat = _nasdaq_catalog(request)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product_modes": [
            {
                "id": "LIVE_MARKET_PAPER",
                "label": "LIVE-MARKET PAPER (NSE INR + NASDAQ USD)",
                "simulated": True,
                "real_money": False,
                "default": True,
                "description": "Local simulated execution driven by live read-only Upstox & Alpaca market data; zero broker order calls.",
            },
            {
                "id": "LIVE_MARKET_LIVE",
                "label": "LIVE-MARKET LIVE (Gated Real-Money Execution)",
                "simulated": False,
                "real_money": True,
                "default": False,
                "description": "Separate broker execution behind distinct credentials, bounded risk limits, fresh preflight, Arm, and Activate.",
            },
        ],
        "paper": {
            "NSE": eng.market_snapshot("NSE"),
            "NASDAQ": eng.market_snapshot("NASDAQ"),
        },
        "live": _live_status_summary(request),
        "readiness": _build_readiness_matrix(request),
        "providers": build_provider_matrix(request.app.state),
        "watchlists": eng.list_watchlists(),
        "reconciliation": {
            "NSE": nse_cat.reconciliation_report(),
            "NASDAQ": nasdaq_cat.reconciliation_report(),
        },
        "recent_audit": eng.audit_log(limit=40),
    }


@router.get("/search")
def search_instruments(
    request: Request,
    query: str = Query(default="", max_length=80),
    market: str = Query(default="ALL", pattern="^(ALL|NSE|NASDAQ|all|nse|nasdaq)$"),
    subclass: str = Query(default="", max_length=40),
    include_indices: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
):
    mkt = market.upper()
    out_nse = None
    out_nasdaq = None
    combined: list[dict[str, Any]] = []

    if mkt in {"ALL", "NSE"}:
        nse_cat = _nse_search_catalog(request)
        out_nse = nse_cat.search(
            query,
            subclass=subclass,
            include_indices=include_indices,
            limit=limit,
        )
        for row in out_nse["items"]:
            combined.append({**row, "market": "NSE", "venue": row.get("exchange", "NSE")})

    if mkt in {"ALL", "NASDAQ"}:
        nasdaq_cat = _nasdaq_catalog(request)
        out_nasdaq = nasdaq_cat.search(query, subclass=subclass, limit=limit)
        for row in out_nasdaq["items"]:
            combined.append({**row, "market": "NASDAQ", "venue": row.get("exchange", "NASDAQ")})

    return {
        "query": query,
        "market": mkt,
        "items": combined[:limit],
        "nse": {
            "source_kind": _nse_search_catalog(request).source_kind,
            "ambiguous": out_nse["ambiguous"] if out_nse else False,
            "ambiguity_detail": out_nse["ambiguity_detail"] if out_nse else None,
            "total_matches": out_nse["total_matches"] if out_nse else 0,
        }
        if out_nse is not None
        else None,
        "nasdaq": {
            "source_kind": _nasdaq_catalog(request).source_kind,
            "total_matches": out_nasdaq["total_matches"] if out_nasdaq else 0,
        }
        if out_nasdaq is not None
        else None,
    }


@router.get("/reconciliation")
def market_reconciliation(request: Request):
    return {
        "NSE": _nse_search_catalog(request).reconciliation_report(),
        "NASDAQ": _nasdaq_catalog(request).reconciliation_report(),
    }


@router.post("/nse/refresh")
def refresh_nse_bod(request: Request):
    autonomy = getattr(request.app.state, "autonomy", None)
    if autonomy is None:
        raise HTTPException(503, "Upstox BOD catalog service is unavailable")
    try:
        autonomy.refresh_catalog()
    except Exception as exc:
        raise HTTPException(502, f"Upstox BOD master download failed: {str(exc)[:220]}") from exc
    return autonomy.catalog.reconciliation_report()


@router.post("/nasdaq/sync")
def sync_nasdaq_catalog(request: Request):
    creds, _ = _alpaca_readonly_creds(request)
    if not creds["api_key"] or not creds["api_secret"]:
        raise HTTPException(
            409,
            "Save Alpaca Market Data API Key & Secret in Connections before syncing the full NASDAQ universe.",
        )
    cat = _nasdaq_catalog(request)
    try:
        return cat.sync_from_alpaca(creds["api_key"], creds["api_secret"], exchange="NASDAQ")
    except Exception as exc:
        raise HTTPException(502, f"Alpaca NASDAQ asset sync failed: {str(exc)[:220]}") from exc


@router.get("/watchlists")
def get_watchlists(request: Request):
    return {"watchlists": _paper_engine(request).list_watchlists()}


@router.post("/watchlists")
def add_watchlist(body: WatchlistItemBody, request: Request):
    try:
        return _paper_engine(request).add_watchlist_symbol(body.market, body.symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/watchlists/{market}/{symbol:path}")
def remove_watchlist(market: str, symbol: str, request: Request):
    try:
        return _paper_engine(request).remove_watchlist_symbol(market, symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/quote")
def live_readonly_quote(
    request: Request,
    market: str = Query(pattern="^(NSE|NASDAQ|nse|nasdaq)$"),
    symbol: str = Query(min_length=1, max_length=64),
):
    mkt = market.upper()
    if mkt == "NSE":
        cat = _nse_search_catalog(request)
        searched = cat.search(symbol, include_indices=True, limit=1)
        if not searched["items"]:
            raise HTTPException(404, f"Unknown NSE symbol or key: {symbol}")
        inst = searched["items"][0]
        tok, src = _upstox_readonly_token(request)
        ver = verify_upstox_market_data(tok, credential_source=src, instrument_key=inst["instrument_key"])
        return {
            "market": "NSE",
            "currency": "INR",
            "instrument": inst,
            "connection_state": ver["connection_state"],
            "authenticated": ver["authenticated"],
            "session": ver["session"],
            "quote": ver.get("quote"),
            "guidance": ver.get("guidance"),
            "real_order_api_used": False,
        }

    cat = _nasdaq_catalog(request)
    inst_obj = cat.get(symbol)
    sym_clean = inst_obj.symbol if inst_obj else validate_us_symbol(symbol.strip().upper())
    creds, src = _alpaca_readonly_creds(request)
    ver = verify_alpaca_market_data(
        creds["api_key"],
        creds["api_secret"],
        feed=creds["feed"],
        credential_source=src,
        symbol=sym_clean,
    )
    return {
        "market": "NASDAQ",
        "currency": "USD",
        "instrument": inst_obj.public() if inst_obj else {"symbol": sym_clean, "exchange": "NASDAQ", "currency": "USD"},
        "connection_state": ver["connection_state"],
        "authenticated": ver["authenticated"],
        "session": ver["session"],
        "quote": ver.get("quote"),
        "guidance": ver.get("guidance"),
        "real_order_api_used": False,
    }


@router.get("/chart")
def live_readonly_chart(
    request: Request,
    market: str = Query(pattern="^(NSE|NASDAQ|nse|nasdaq)$"),
    symbol: str = Query(min_length=1, max_length=64),
    timeframe: str = Query(default="5m"),
):
    """Fetch real read-only OHLCV bars for NSE (Upstox) or NASDAQ (Alpaca).

    Never substitutes synthetic/replay candles as live market data when credentials
    are missing or offline — returns explicit connection_state and empty bars with
    actionable guidance.
    """
    if timeframe not in TIMEFRAMES:
        raise HTTPException(422, f"Unsupported timeframe {timeframe!r}")
    mkt = market.upper()
    now = datetime.now(timezone.utc)

    if mkt == "NSE":
        cat = _nse_search_catalog(request)
        searched = cat.search(symbol, include_indices=True, limit=1)
        if not searched["items"]:
            raise HTTPException(404, f"Unknown NSE symbol: {symbol}")
        inst = searched["items"][0]
        sess = nse_session_status(now)
        tok, src = _upstox_readonly_token(request)
        if not tok:
            return {
                "ok": False,
                "market": "NSE",
                "currency": "INR",
                "symbol": inst["trading_symbol"],
                "instrument": inst,
                "timeframe": timeframe,
                "connection_state": "DISCONNECTED",
                "session": sess,
                "data_source": "upstox-market-data-v3 (DISCONNECTED)",
                "data_feed_label": "UPSTOX DISCONNECTED · SAVE DAILY ACCESS TOKEN FOR REAL NSE BARS (NO SAMPLE FALLBACK)",
                "bars": [],
                "quote": None,
                "simulated_bars": False,
                "real_order_api_used": False,
                "generated_at": now.isoformat(),
            }
        reader = UpstoxReadOnly(lambda: tok, clock=lambda: now)
        bars = []
        error_detail = None
        try:
            if timeframe != "1D" and sess["is_open"]:
                bars = reader.intraday_candles(inst["instrument_key"], inst["display_symbol"], timeframe)
            if not bars:
                bars = reader.historical_candles(inst["instrument_key"], inst["display_symbol"], timeframe)
        except (UpstoxAuthError, ReconciliationFailure, ValueError) as exc:
            error_detail = str(exc)[:220]
        quote_ver = verify_upstox_market_data(tok, credential_source=src, instrument_key=inst["instrument_key"], now=now)
        conn_state = quote_ver["connection_state"] if bars or quote_ver.get("quote") else ("REJECTED" if "rejected" in (error_detail or "").lower() else "UNAVAILABLE")
        return {
            "ok": bool(bars),
            "market": "NSE",
            "currency": "INR",
            "symbol": inst["trading_symbol"],
            "instrument": inst,
            "timeframe": timeframe,
            "connection_state": conn_state,
            "session": sess,
            "data_source": "upstox-historical-v3",
            "data_feed_label": (
                f"UPSTOX NSE V3 · {'LIVE SESSION' if sess['is_open'] else sess['state']} · READ-ONLY MARKET DATA"
            ),
            "bars": [b.to_dict() for b in bars[-300:]],
            "quote": quote_ver.get("quote"),
            "error": error_detail,
            "simulated_bars": False,
            "real_order_api_used": False,
            "generated_at": now.isoformat(),
        }

    # NASDAQ via read-only Alpaca Market Data
    if timeframe == "4h":
        timeframe_fetch = "1h"
    else:
        timeframe_fetch = timeframe
    cat = _nasdaq_catalog(request)
    inst_obj = cat.get(symbol)
    try:
        sym_clean = inst_obj.symbol if inst_obj else validate_us_symbol(symbol.strip().upper())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    us_cal = USEquitiesCalendar()
    sess = us_cal.market_state(now)
    creds, src = _alpaca_readonly_creds(request)
    if not creds["api_key"] or not creds["api_secret"]:
        return {
            "ok": False,
            "market": "NASDAQ",
            "currency": "USD",
            "symbol": sym_clean,
            "instrument": inst_obj.public() if inst_obj else {"symbol": sym_clean, "exchange": "NASDAQ", "currency": "USD"},
            "timeframe": timeframe,
            "connection_state": "DISCONNECTED",
            "session": sess,
            "data_source": "alpaca-market-data (DISCONNECTED)",
            "data_feed_label": "ALPACA MARKET DATA DISCONNECTED · SAVE API KEY & SECRET FOR REAL NASDAQ BARS (NO SAMPLE FALLBACK)",
            "bars": [],
            "quote": None,
            "simulated_bars": False,
            "real_order_api_used": False,
            "generated_at": now.isoformat(),
        }

    lookback_days = 240 if timeframe_fetch == "1D" else (30 if timeframe_fetch == "1h" else 7)
    start = now - timedelta(days=lookback_days)
    bars = []
    error_detail = None
    hist = AlpacaHistoricalProvider(creds["api_key"], creds["api_secret"], feed=creds["feed"])
    try:
        bars = hist.get_candles(sym_clean, timeframe_fetch, start, now)
    except Exception as exc:
        error_detail = str(exc)[:220]
    finally:
        hist.close()

    quote_ver = verify_alpaca_market_data(
        creds["api_key"],
        creds["api_secret"],
        feed=creds["feed"],
        credential_source=src,
        symbol=sym_clean,
        now=now,
    )
    conn_state = quote_ver["connection_state"] if (bars or quote_ver.get("quote")) else "UNAVAILABLE"
    return {
        "ok": bool(bars),
        "market": "NASDAQ",
        "currency": "USD",
        "symbol": sym_clean,
        "instrument": inst_obj.public() if inst_obj else {"symbol": sym_clean, "exchange": "NASDAQ", "currency": "USD"},
        "timeframe": timeframe,
        "connection_state": conn_state,
        "session": sess,
        "feed": creds["feed"],
        "data_source": f"alpaca-market-data:{creds['feed']}",
        "data_feed_label": f"ALPACA {creds['feed'].upper()} · {feed_label(creds['feed'])} · {sess['status']}",
        "bars": [b.to_dict() for b in bars[-300:]],
        "quote": quote_ver.get("quote"),
        "error": error_detail,
        "simulated_bars": False,
        "real_order_api_used": False,
        "generated_at": now.isoformat(),
    }


# -- Live-Market Paper Endpoints -----------------------------------------------


@router.get("/paper/{market}")
def paper_snapshot(market: str, request: Request):
    try:
        return _paper_engine(request).market_snapshot(market)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/session/start")
def paper_session_start(body: PaperSessionStartBody, request: Request):
    try:
        return _paper_engine(request).start_session(
            body.market,
            strategy=body.strategy,
            timeframe=body.timeframe,
            symbols=body.symbols,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/session/transition")
def paper_session_transition(body: PaperSessionTransitionBody, request: Request):
    try:
        return _paper_engine(request).transition_session(body.market, body.action, reason=body.reason)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/reset")
def paper_reset(body: PaperResetBody, request: Request):
    try:
        return _paper_engine(request).reset_paper_account(body.market, confirm=body.confirm)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/config")
def paper_config_update(body: PaperConfigBody, request: Request):
    updates = body.model_dump(exclude={"market"}, exclude_none=True)
    try:
        return {"ok": True, "config": _paper_engine(request).update_config(body.market, **updates)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/preview")
def paper_preview(body: PaperOrderBody, request: Request):
    return _paper_engine(request).preview_order(
        market=body.market,
        symbol=body.symbol,
        side=body.side,
        qty=body.qty,
        order_type=body.order_type,
        product=body.product,
        time_in_force=body.time_in_force,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        allow_off_hours_working=body.allow_off_hours_working,
    )


@router.post("/paper/orders")
def paper_submit_order(body: PaperOrderBody, request: Request):
    return _paper_engine(request).submit_order(
        client_order_id=body.client_order_id,
        market=body.market,
        symbol=body.symbol,
        side=body.side,
        qty=body.qty,
        order_type=body.order_type,
        product=body.product,
        time_in_force=body.time_in_force,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        allow_off_hours_working=body.allow_off_hours_working,
    )


@router.delete("/paper/orders/{client_order_id}")
def paper_cancel_order(client_order_id: str, request: Request):
    try:
        return _paper_engine(request).cancel_order(client_order_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/paper/orders/{client_order_id}")
def paper_replace_order(client_order_id: str, body: PaperReplaceBody, request: Request):
    try:
        return _paper_engine(request).replace_order(
            client_order_id,
            qty=body.qty,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/positions/protection")
def paper_modify_protection(body: PaperProtectionBody, request: Request):
    try:
        return _paper_engine(request).modify_position_protection(
            body.market,
            body.symbol,
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/positions/{market}/{symbol:path}/close")
def paper_close_position(market: str, symbol: str, request: Request):
    try:
        return _paper_engine(request).close_position(market, symbol)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/{market}/sync")
def paper_sync(market: str, request: Request):
    try:
        return _paper_engine(request).sync_live_tick(market)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/{market}/reconcile")
def paper_reconcile(market: str, request: Request):
    try:
        return _paper_engine(request).reconcile(market)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/paper/audit/events")
def paper_audit(request: Request, market: str = "", limit: int = Query(default=150, ge=1, le=500)):
    return {"events": _paper_engine(request).audit_log(market=market, limit=limit)}


# -- Live Status & Atomic Global Emergency Stop --------------------------------


@router.get("/live/status")
def live_status(request: Request):
    return _live_status_summary(request)


@router.post("/emergency-stop")
def global_emergency_stop(body: GlobalEmergencyBody, request: Request):
    """Atomic Emergency Stop across Local Paper, Upstox Autonomy, Alpaca Broker, and Vantage."""
    reason = body.reason or "operator global emergency stop"
    results: dict[str, Any] = {}

    eng = getattr(request.app.state, "live_paper_engine", None)
    if eng is not None:
        results["live_market_paper"] = eng.emergency_stop(reason)

    autonomy = getattr(request.app.state, "autonomy", None)
    if autonomy is not None:
        try:
            active_sessions = [
                s for s in autonomy.sessions()
                if isinstance(s, dict) and s.get("session_id") and s.get("state") in {"ARMED", "ACTIVE"}
            ]
            if active_sessions:
                results["upstox_autonomy"] = [
                    autonomy.emergency_stop(str(s["session_id"]), reason=reason, cancel_entries=True)
                    for s in active_sessions
                ]
            else:
                results["upstox_autonomy"] = {"ok": True, "detail": "no active autonomy session"}
        except Exception as exc:
            results["upstox_autonomy"] = {"ok": False, "detail": str(exc)[:180]}

    alpaca_svc = getattr(request.app.state, "alpaca_broker", None)
    if alpaca_svc is not None:
        results["alpaca_broker"] = alpaca_svc.emergency_stop(reason)

    vantage = getattr(request.app.state, "vantage", None)
    if vantage is not None:
        try:
            results["vantage"] = vantage.emergency_stop(reason=reason, cancel_pending=True)
        except Exception as exc:
            results["vantage"] = {"ok": False, "detail": str(exc)[:180]}

    return {"ok": True, "reason": reason, "subsystems": results}


@router.post("/emergency-stop/reset")
def global_emergency_stop_reset(body: GlobalResetBody, request: Request):
    if (body.confirm_phrase or "").strip() != "CLEAR EMERGENCY STOP":
        raise HTTPException(422, "Typed confirmation phrase 'CLEAR EMERGENCY STOP' is required")
    results: dict[str, Any] = {}
    eng = getattr(request.app.state, "live_paper_engine", None)
    if eng is not None:
        results["live_market_paper"] = eng.reset_emergency_stop()
    alpaca_svc = getattr(request.app.state, "alpaca_broker", None)
    if alpaca_svc is not None and alpaca_svc.gate.kill_engaged:
        results["alpaca_broker"] = alpaca_svc.gate.reset_kill_switch(operator_ack=True)
    vantage = getattr(request.app.state, "vantage", None)
    if vantage is not None:
        try:
            results["vantage"] = vantage.clear_emergency_stop(confirmation="CLEAR EMERGENCY STOP")
        except Exception as exc:
            results["vantage"] = {"ok": False, "detail": str(exc)[:180]}
    return {"ok": True, "live_execution_remains_off": True, "subsystems": results}


# ---------------------------------------------------------------------------
# Read-Only Live Trade Monitor (`11 · Live Trade Monitor`, 0 order endpoints)
# ---------------------------------------------------------------------------


@router.get("/trade-monitor")
def get_trade_monitor(
    request: Request,
    market: str = Query(default="ALL"),
    execution_mode: str = Query(default="ALL"),
    state: str = Query(default="ALL"),
    search: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    from app.markets.trade_monitor import build_trade_monitor_summary

    return build_trade_monitor_summary(
        paper_engine=_paper_engine(request),
        ensemble_service=getattr(request.app.state, "ensemble_service", None),
        alpaca_service=getattr(request.app.state, "alpaca_broker", None),
        market_filter=market,
        mode_filter=execution_mode,
        state_filter=state,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/trade-monitor/chart")
def get_trade_monitor_chart(
    request: Request,
    market: str = Query(pattern="^(NSE|NASDAQ|nse|nasdaq)$"),
    symbol: str = Query(min_length=1, max_length=64),
    timeframe: str = Query(default="5m"),
    execution_mode: str = Query(default="LIVE_MARKET_PAPER"),
):
    """Read-only candlestick + 8-shape event marker payload for Live Trade Monitor.

    Never substitutes sample/demo bars when market data is missing, stale, or
    disconnected, and never invokes any broker order creation/cancel endpoint.
    """
    from app.markets.trade_monitor import build_instrument_event_markers

    chart_payload = live_readonly_chart(
        request=request,
        market=market,
        symbol=symbol,
        timeframe=timeframe,
    )
    bars_list = chart_payload.get("bars") or []
    latest_bar_ts = str(bars_list[-1]["timestamp"]) if bars_list else None
    marker_payload = build_instrument_event_markers(
        market=chart_payload.get("market") or market,
        symbol=chart_payload.get("symbol") or symbol,
        execution_mode=execution_mode,
        paper_engine=_paper_engine(request),
        ensemble_service=getattr(request.app.state, "ensemble_service", None),
        alpaca_service=getattr(request.app.state, "alpaca_broker", None),
        latest_bar_ts=latest_bar_ts,
    )
    return {
        "read_only": True,
        "broker_order_api_calls": 0,
        **chart_payload,
        "execution_mode": marker_payload["execution_mode"],
        "simulated_execution": marker_payload["simulated"],
        "session_banner": marker_payload["session_banner"],
        "reference_lines": marker_payload["reference_lines"],
        "markers": marker_payload["markers"],
    }

