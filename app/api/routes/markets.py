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
from app.markets.nse_reconciliation import nse_session_status, resolve_nse_instrument_strict
from app.markets.us_calendar import USEquitiesCalendar

router = APIRouter(prefix="/api/markets")


class WatchlistItemBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    symbol: str = Field(min_length=1, max_length=64)
    instrument_key: str | None = Field(default=None, max_length=64)


class PaperSessionStartBody(BaseModel):
    market: str = Field(pattern="^(NSE|NASDAQ|nse|nasdaq)$")
    strategy: str = Field(default="manual_only", max_length=40)
    timeframe: str = Field(default="5m", max_length=8)
    symbols: list[str] = Field(default_factory=list)
    kronos_enabled: bool | None = None
    kronos_failure_policy: str | None = Field(default=None, max_length=40)


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
    instrument_key: str | None = Field(default=None, max_length=64)
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
    instrument_key: str | None = Field(default=None, max_length=64)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)


class GlobalEmergencyBody(BaseModel):
    reason: str = Field(default="operator global emergency stop", max_length=200)


class GlobalResetBody(BaseModel):
    confirm_phrase: str = Field(default="", max_length=80)


class MultiMarketSelectionBody(BaseModel):
    markets: list[str] = Field(min_length=1, max_length=4)


class MultiMarketSessionStartBody(BaseModel):
    markets: list[str] | None = None
    default_strategy: str = Field(default="parallel_ensemble", max_length=64)
    default_timeframe: str = Field(default="5m", max_length=8)
    strategy_by_market: dict[str, str] = Field(default_factory=dict)
    timeframe_by_market: dict[str, str] = Field(default_factory=dict)
    kronos_enabled_by_market: dict[str, bool] = Field(default_factory=dict)
    kronos_policy_by_market: dict[str, str] = Field(default_factory=dict)
    execute_immediately: bool = True


class MultiMarketTransitionBody(BaseModel):
    action: str = Field(pattern="^(start|pause|resume|stop)$")
    markets: list[str] | None = None
    reason: str = Field(default="", max_length=200)
    execute_immediately: bool = True


class MultiMarketAnalyzeBody(BaseModel):
    markets: list[str] | None = None


class MultiMarketRunCycleBody(BaseModel):
    markets: list[str] | None = None
    only_active: bool = True
    market_timeout_seconds: float = Field(default=12.0, ge=0.1, le=60.0)


def _paper_engine(request: Request):
    eng = getattr(request.app.state, "live_paper_engine", None)
    if eng is None:
        raise HTTPException(503, "Live-Market Paper engine is not initialized")
    return eng


def _live_coordinator(request: Request):
    coord = getattr(request.app.state, "live_execution", None)
    if coord is None:
        raise HTTPException(503, "Live execution coordinator is not initialized")
    return coord


def _ensure_paper_routing(request: Request, market: str) -> None:
    """Refuse to run local paper simulation for a market routed LIVE.

    Execution paths are separate by design: a market is either simulated locally
    (PAPER) or routed to its real broker (LIVE), never both, and never silently.
    """
    coord = getattr(request.app.state, "live_execution", None)
    if coord is None:
        return
    if not coord.paper_execution_allowed(market):
        raise HTTPException(
            409,
            f"{market} is routed to LIVE execution. Local paper simulation does not run for "
            "this market (mode separation). Use the live endpoints or set mode back to PAPER.",
        )


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
    nse_algo_name = bool(
        str(getattr(getattr(autonomy, "settings", None), "upstox_algo_name", "") or "").strip()
    )

    broker_rt = getattr(request.app.state, "alpaca_broker_runtime", None)
    alpaca_svc = getattr(request.app.state, "alpaca_broker", None) or (broker_rt.service if broker_rt else None)
    alpaca_creds = broker_rt.credential_status() if broker_rt else None
    us_live_flag = bool(request.app.state.settings.alpaca_broker_enabled)
    us_live_creds = bool(alpaca_creds and alpaca_creds["live"]["present"])
    coordinator = getattr(request.app.state, "live_execution", None)
    live_binding = coordinator.status() if coordinator is not None else {}

    return {
        "default_live_execution": "OFF",
        "execution_paths_are_separate": True,
        "modes": {
            "LIVE_MARKET_PAPER": "local simulated orders on real read-only market data",
            "LIVE_MARKET_LIVE": "real broker orders, per-market Arm + phrase-confirmed Activate",
        },
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
            "execution_mode": (live_binding.get("NSE") or {}).get("mode", "PAPER"),
            "live_gate_state": ((live_binding.get("NSE") or {}).get("gate") or {}).get("state", "DISABLED"),
            "live_order_path_available": bool(
                (live_binding.get("NSE") or {}).get("live_available")
            ),
            "missing_coordinator_dependencies": (live_binding.get("NSE") or {}).get(
                "missing_dependencies", []
            ),
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
            "execution_mode": (live_binding.get("NASDAQ") or {}).get("mode", "PAPER"),
            "live_gate_state": ((live_binding.get("NASDAQ") or {}).get("gate") or {}).get(
                "state", "DISABLED"
            ),
            "live_order_path_available": bool(
                (live_binding.get("NASDAQ") or {}).get("live_available")
            ),
            "missing_coordinator_dependencies": (live_binding.get("NASDAQ") or {}).get(
                "missing_dependencies", []
            ),
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


def _build_paper_modes_catalog(request: Request) -> list[dict[str, Any]]:
    """Explicit runtime attribution across all 3 supported PAPER modes."""
    eng = _paper_engine(request)
    nse_snap = eng.market_snapshot("NSE")
    us_snap = eng.market_snapshot("NASDAQ")

    sim = getattr(request.app.state, "simulator", None)
    sim_status = sim.status() if sim is not None and hasattr(sim, "status") else {}
    sim_state = str(sim_status.get("state") or "STOPPED")
    sim_auto = bool(sim_status.get("automation_enabled", True))
    sim_cur_run = sim_status.get("current_run") or {}

    broker_rt = getattr(request.app.state, "alpaca_broker_runtime", None)
    broker_creds = broker_rt.credential_status() if broker_rt is not None else None
    alpaca_svc = getattr(request.app.state, "alpaca_broker", None) or (broker_rt.service if broker_rt else None)
    alpaca_b_stat = alpaca_svc.status() if alpaca_svc is not None else {}
    alpaca_paper_active = bool(alpaca_b_stat.get("enabled") and alpaca_b_stat.get("environment") == "paper")

    return [
        {
            "mode_id": "LOCAL_REPLAY_PAPER",
            "engine_id": "AXIOM_PAPER",
            "label": "1 · Local Replay Paper (Labeled Sample CSV Data)",
            "market_data_source": "BUNDLED_SAMPLE_CSV_REPLAY (data/sample*.csv — NOT live market data)",
            "execution_destination": "LOCAL_SQLITE_PAPER_BROKER (zero external network or broker calls)",
            "automation_state": "ENABLED_ON_REPLAY_BARS" if sim_auto else "MANUAL_ONLY",
            "session_lifecycle": sim_state,
            "fill_type": "SIMULATED",
            "real_money": False,
            "current_run_pnl_isolated": True,
            "current_run": sim_cur_run if sim_cur_run else None,
        },
        {
            "mode_id": "LIVE_MARKET_LOCAL_PAPER",
            "engine_id": "LIVE_MARKET_PAPER",
            "label": "2 · Live-Market Local PAPER (Real Read-Only Upstox/Alpaca Data + Local Simulated Fills)",
            "market_data_source": "READ_ONLY_LIVE_FEEDS (Upstox NSE v2/v3 + Alpaca NASDAQ IEX/SIP; zero sample fallback)",
            "execution_destination": "LOCAL_SQLITE_SIMULATOR (live_market_paper.sqlite3 — zero broker order calls)",
            "automation_state": {
                "NSE": nse_snap["automation_state"],
                "NASDAQ": us_snap["automation_state"],
            },
            "session_lifecycle": {
                "NSE": nse_snap["session_state"],
                "NASDAQ": us_snap["session_state"],
            },
            "fill_type": "SIMULATED",
            "real_money": False,
            "current_run_pnl_isolated": True,
            "pnl_attribution": {
                "NSE": nse_snap["pnl_attribution"],
                "NASDAQ": us_snap["pnl_attribution"],
            },
        },
        {
            "mode_id": "ALPACA_BROKER_PAPER",
            "engine_id": "ALPACA_PAPER_SANDBOX",
            "label": "3 · Broker Paper (Alpaca Paper-API Sandbox Endpoint)",
            "market_data_source": "READ_ONLY_ALPACA_MARKET_DATA (isolated ALPACA_MARKET_DATA credential slot)",
            "execution_destination": "ALPACA_BROKER_PAPER_SANDBOX_ENDPOINT (isolated ALPACA_BROKER_PAPER credential slot)",
            "automation_state": "MANUAL_AND_EXPLICIT_OPERATOR_SUBMIT_ONLY",
            "session_lifecycle": "ENABLED_PAPER_SANDBOX" if alpaca_paper_active else "DISABLED",
            "credential_present": bool(broker_creds and broker_creds["paper"]["present"]),
            "fill_type": "BROKER_PAPER_SANDBOX_CONFIRMED",
            "real_money": False,
            "current_run_pnl_isolated": True,
        },
    ]


@router.get("/overview")
def markets_overview(request: Request):
    eng = _paper_engine(request)
    nse_cat = _nse_search_catalog(request)
    nasdaq_cat = _nasdaq_catalog(request)
    runner = getattr(request.app.state, "mm_paper_runner", None)
    live_runner = getattr(request.app.state, "mm_live_runner", None)
    coordinator = getattr(request.app.state, "live_execution", None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selected_markets": eng.selected_markets(),
        "runner": runner.status() if runner is not None else None,
        "live_runner": live_runner.status() if live_runner is not None else None,
        "live_execution": coordinator.live_status_summary() if coordinator is not None else None,
        "execution_modes": (
            {m: coordinator.mode(m) for m in ("NSE", "NASDAQ")} if coordinator is not None
            else {"NSE": "PAPER", "NASDAQ": "PAPER"}
        ),
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
        "paper_modes": _build_paper_modes_catalog(request),
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
        "kronos": (
            request.app.state.kronos_provider.status()
            if getattr(request.app.state, "kronos_provider", None) is not None
            else None
        ),
        "recent_audit": eng.audit_log(limit=40),
        "live_audit": (
            coordinator.journal.audit_log(limit=25) if coordinator is not None else []
        ),
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
            segment="NSE_EQ",
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
    target = (body.instrument_key or body.symbol) if body.market.upper() == "NSE" else body.symbol
    try:
        return _paper_engine(request).add_watchlist_symbol(body.market, target)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/watchlists/{market}/{symbol:path}")
def remove_watchlist(market: str, symbol: str, request: Request):
    try:
        return _paper_engine(request).remove_watchlist_symbol(market, symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _resolve_nse_for_route(cat: InstrumentCatalog, target: str, *, allow_index: bool = True) -> dict[str, Any]:
    try:
        return resolve_nse_instrument_strict(cat, target, allow_index=allow_index)
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("Rejected "):
            raise HTTPException(422, msg) from exc
        raise HTTPException(404, msg) from exc


@router.get("/quote")
def live_readonly_quote(
    request: Request,
    market: str = Query(pattern="^(NSE|NASDAQ|nse|nasdaq)$"),
    symbol: str = Query(min_length=1, max_length=64),
    instrument_key: str = Query(default="", max_length=64),
):
    mkt = market.upper()
    now = datetime.now(timezone.utc)
    ikey_str = instrument_key.strip() if isinstance(instrument_key, str) else ""
    if mkt == "NSE":
        cat = _nse_search_catalog(request)
        inst = _resolve_nse_for_route(cat, ikey_str or symbol, allow_index=True)
        tok, src = _upstox_readonly_token(request)
        ver = verify_upstox_market_data(tok, credential_source=src, instrument_key=inst["instrument_key"], now=now)
        q_obj = ver.get("quote") or {}
        return {
            "market": "NSE",
            "currency": "INR",
            "instrument": inst,
            "instrument_key": inst["instrument_key"],
            "exchange": inst.get("exchange", "NSE"),
            "connection_state": ver["connection_state"],
            "authenticated": ver["authenticated"],
            "provider": "upstox_market_data",
            "feed": ver.get("feed") or "upstox_nse_v2",
            "feed_entitlement_label": ver.get("feed_entitlement_label") or "Upstox NSE Cash Read-Only Feed",
            "source_timestamp": q_obj.get("timestamp"),
            "local_received_at": ver.get("checked_at") or now.isoformat(),
            "quote_age_seconds": q_obj.get("age_seconds"),
            "market_session_status": ver["session"]["state"],
            "session": ver["session"],
            "quote": ver.get("quote"),
            "guidance": ver.get("guidance"),
            "simulated_fallback_used": False,
            "real_order_api_used": False,
        }

    cat = _nasdaq_catalog(request)
    inst_obj = cat.get(symbol)
    try:
        sym_clean = inst_obj.symbol if inst_obj else validate_us_symbol(symbol.strip().upper())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    creds, src = _alpaca_readonly_creds(request)
    ver = verify_alpaca_market_data(
        creds["api_key"],
        creds["api_secret"],
        feed=creds["feed"],
        credential_source=src,
        symbol=sym_clean,
        now=now,
    )
    inst_pub = inst_obj.public() if inst_obj else {
        "symbol": sym_clean,
        "trading_symbol": sym_clean,
        "instrument_key": f"NASDAQ|{sym_clean}",
        "exchange": "NASDAQ",
        "currency": "USD",
    }
    q_obj = ver.get("quote") or {}
    return {
        "market": "NASDAQ",
        "currency": "USD",
        "instrument": inst_pub,
        "instrument_key": inst_pub.get("instrument_key") or f"NASDAQ|{sym_clean}",
        "exchange": inst_pub.get("exchange", "NASDAQ"),
        "connection_state": ver["connection_state"],
        "authenticated": ver["authenticated"],
        "provider": "alpaca_market_data",
        "feed": creds["feed"],
        "feed_entitlement_label": feed_label(creds["feed"]),
        "feed_is_consolidated": creds["feed"] == "sip",
        "source_timestamp": q_obj.get("timestamp"),
        "local_received_at": ver.get("checked_at") or now.isoformat(),
        "quote_age_seconds": q_obj.get("age_seconds"),
        "market_session_status": ver["session"]["status"],
        "session": ver["session"],
        "quote": ver.get("quote"),
        "guidance": ver.get("guidance"),
        "simulated_fallback_used": False,
        "real_order_api_used": False,
    }


@router.get("/chart")
def live_readonly_chart(
    request: Request,
    market: str = Query(pattern="^(NSE|NASDAQ|nse|nasdaq)$"),
    symbol: str = Query(min_length=1, max_length=64),
    instrument_key: str = Query(default="", max_length=64),
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
    ikey_str = instrument_key.strip() if isinstance(instrument_key, str) else ""

    if mkt == "NSE":
        cat = _nse_search_catalog(request)
        inst = _resolve_nse_for_route(cat, ikey_str or symbol, allow_index=True)
        sess = nse_session_status(now)
        tok, src = _upstox_readonly_token(request)
        ent_lbl = "Upstox NSE Cash Read-Only Feed (NSE_EQ)"
        if not tok:
            return {
                "ok": False,
                "market": "NSE",
                "currency": "INR",
                "symbol": inst["trading_symbol"],
                "instrument_key": inst["instrument_key"],
                "exchange": inst.get("exchange", "NSE"),
                "instrument": inst,
                "timeframe": timeframe,
                "connection_state": "DISCONNECTED",
                "provider": "upstox_market_data",
                "feed": "upstox_nse_v3",
                "feed_entitlement_label": ent_lbl,
                "source_timestamp": None,
                "local_received_at": now.isoformat(),
                "bar_age_seconds": None,
                "market_session_status": sess["state"],
                "session": sess,
                "data_source": "upstox-market-data-v3 (DISCONNECTED)",
                "data_feed_label": "UPSTOX DISCONNECTED · SAVE DAILY ACCESS TOKEN FOR REAL NSE BARS (NO SAMPLE FALLBACK)",
                "bars": [],
                "quote": None,
                "simulated_bars": False,
                "real_order_api_used": False,
                "generated_at": now.isoformat(),
            }
        from app.monitoring.paper_events import redact_sensitive_data

        reader = UpstoxReadOnly(lambda: tok, clock=lambda: now)
        bars = []
        error_detail = None
        try:
            if timeframe != "1D" and sess["is_open"]:
                bars = reader.intraday_candles(inst["instrument_key"], inst["display_symbol"], timeframe)
            if not bars:
                bars = reader.historical_candles(inst["instrument_key"], inst["display_symbol"], timeframe)
        except Exception as exc:
            error_detail = str(redact_sensitive_data(str(exc)))[:220]
        quote_ver = verify_upstox_market_data(tok, credential_source=src, instrument_key=inst["instrument_key"], now=now)
        conn_state = quote_ver["connection_state"] if bars or quote_ver.get("quote") else ("REJECTED" if "rejected" in (error_detail or "").lower() else "UNAVAILABLE")
        last_bar_ts = bars[-1].timestamp if bars else None
        bar_age = round(max(0.0, (now - last_bar_ts).total_seconds()), 2) if last_bar_ts else None
        q_obj = quote_ver.get("quote") or {}
        return {
            "ok": bool(bars),
            "market": "NSE",
            "currency": "INR",
            "symbol": inst["trading_symbol"],
            "instrument_key": inst["instrument_key"],
            "exchange": inst.get("exchange", "NSE"),
            "instrument": inst,
            "timeframe": timeframe,
            "connection_state": conn_state,
            "provider": "upstox_market_data",
            "feed": "upstox_nse_v3",
            "feed_entitlement_label": ent_lbl,
            "source_timestamp": q_obj.get("timestamp") or (last_bar_ts.isoformat() if last_bar_ts else None),
            "local_received_at": now.isoformat(),
            "quote_age_seconds": q_obj.get("age_seconds"),
            "bar_age_seconds": bar_age,
            "market_session_status": sess["state"],
            "session": sess,
            "data_source": "upstox-historical-v3",
            "data_feed_label": (
                f"UPSTOX NSE V3 · {inst['instrument_key']} · {'LIVE SESSION' if sess['is_open'] else sess['state']} · READ-ONLY MARKET DATA"
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
    inst_pub_us = inst_obj.public() if inst_obj else {
        "symbol": sym_clean,
        "trading_symbol": sym_clean,
        "instrument_key": f"NASDAQ|{sym_clean}",
        "exchange": "NASDAQ",
        "currency": "USD",
    }
    if not creds["api_key"] or not creds["api_secret"]:
        return {
            "ok": False,
            "market": "NASDAQ",
            "currency": "USD",
            "symbol": sym_clean,
            "instrument_key": inst_pub_us.get("instrument_key") or f"NASDAQ|{sym_clean}",
            "exchange": inst_pub_us.get("exchange", "NASDAQ"),
            "instrument": inst_pub_us,
            "timeframe": timeframe,
            "connection_state": "DISCONNECTED",
            "provider": "alpaca_market_data",
            "feed": creds["feed"],
            "feed_entitlement_label": feed_label(creds["feed"]),
            "feed_is_consolidated": creds["feed"] == "sip",
            "source_timestamp": None,
            "local_received_at": now.isoformat(),
            "bar_age_seconds": None,
            "market_session_status": sess["status"],
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
    from app.monitoring.paper_events import redact_sensitive_data

    hist = AlpacaHistoricalProvider(creds["api_key"], creds["api_secret"], feed=creds["feed"])
    try:
        # AlpacaHistoricalProvider's public API is MarketDataProvider.historical();
        # there is no get_candles() on the adapter.
        bars = hist.historical(sym_clean, timeframe_fetch, start, now)
    except Exception as exc:
        error_detail = str(redact_sensitive_data(str(exc)))[:220]
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
    last_bar_ts_us = bars[-1].timestamp if bars else None
    bar_age_us = round(max(0.0, (now - last_bar_ts_us).total_seconds()), 2) if last_bar_ts_us else None
    q_us = quote_ver.get("quote") or {}
    return {
        "ok": bool(bars),
        "market": "NASDAQ",
        "currency": "USD",
        "symbol": sym_clean,
        "instrument_key": inst_pub_us.get("instrument_key") or f"NASDAQ|{sym_clean}",
        "exchange": inst_pub_us.get("exchange", "NASDAQ"),
        "instrument": inst_pub_us,
        "timeframe": timeframe,
        "connection_state": conn_state,
        "provider": "alpaca_market_data",
        "feed": creds["feed"],
        "feed_entitlement_label": feed_label(creds["feed"]),
        "feed_is_consolidated": creds["feed"] == "sip",
        "source_timestamp": q_us.get("timestamp") or (last_bar_ts_us.isoformat() if last_bar_ts_us else None),
        "local_received_at": now.isoformat(),
        "quote_age_seconds": q_us.get("age_seconds"),
        "bar_age_seconds": bar_age_us,
        "market_session_status": sess["status"],
        "session": sess,
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


def _collect_extra_structured_events(
    request: Request,
    *,
    include_broker_audit: bool = True,
) -> list[dict[str, Any]]:
    from app.monitoring.paper_events import normalize_structured_event

    extras: list[dict[str, Any]] = []
    repo = getattr(request.app.state, "repository", None)
    if repo is not None and hasattr(repo, "list_system_events"):
        try:
            for row in repo.list_system_events(limit=400):
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                kind = str(row.get("kind") or payload.get("kind") or "SYSTEM_EVENT")
                if not (kind.startswith("PAPER_") or kind.startswith("KRONOS_")):
                    continue
                mkt_val = str(payload.get("market") or "LOCAL_SIM").upper()
                extras.append(
                    normalize_structured_event(
                        event_id=str(row.get("id") or row.get("event_id") or f"sim-{len(extras)}"),
                        timestamp=str(row.get("ts") or ""),
                        event_type=kind,
                        market=mkt_val,
                        source="LOCAL_SIMULATOR_PAPER",
                        payload={**payload, "simulated": True, "real_money": False},
                    )
                )
        except Exception as exc:
            extras.append(
                normalize_structured_event(
                    event_id=f"sim-err-{len(extras)}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    event_type="SYSTEM_EVENT_READ_ERROR",
                    market="LOCAL_SIM",
                    source="LOCAL_SIMULATOR_PAPER",
                    payload={"error": type(exc).__name__, "simulated": True, "real_money": False},
                )
            )

    if include_broker_audit:
        alpaca_svc = getattr(request.app.state, "alpaca_broker", None)
        if alpaca_svc is not None and hasattr(alpaca_svc, "status"):
            try:
                alp_st = alpaca_svc.status()
                is_live_env = str(alp_st.get("environment") or "").lower() == "live"
                for ev in (alp_st.get("recent_audit") or [])[:80]:
                    if not isinstance(ev, dict):
                        continue
                    det = dict(ev.get("detail")) if isinstance(ev.get("detail"), dict) else dict(ev)
                    det["simulated"] = not is_live_env
                    det["real_money"] = is_live_env
                    extras.append(
                        normalize_structured_event(
                            event_id=f"alp-{ev.get('seq') or ev.get('id') or len(extras)}",
                            timestamp=str(ev.get("ts") or ev.get("timestamp") or ""),
                            event_type=str(ev.get("kind") or ev.get("event_kind") or "BROKER_AUDIT"),
                            market="NASDAQ",
                            source="ALPACA_LIVE_BROKER" if is_live_env else "ALPACA_PAPER_BROKER",
                            payload=det,
                        )
                    )
            except Exception as exc:
                extras.append(
                    normalize_structured_event(
                        event_id=f"alp-err-{len(extras)}",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        event_type="BROKER_AUDIT_READ_ERROR",
                        market="NASDAQ",
                        source="ALPACA_PAPER_BROKER",
                        payload={"error": type(exc).__name__, "simulated": True, "real_money": False},
                    )
                )

    return extras


@router.get("/paper/logs")
def paper_structured_logs(
    request: Request,
    window: str = Query(default="all", max_length=16),
    since: str = Query(default="", max_length=64),
    until: str = Query(default="", max_length=64),
    market: str = Query(default="", max_length=24),
    symbol: str = Query(default="", max_length=64),
    category: str = Query(default="", max_length=32),
    event_type: str = Query(default="", max_length=64),
    severity: str = Query(default="", max_length=24),
    strategy: str = Query(default="", max_length=64),
    session_id: str = Query(default="", max_length=80),
    query: str = Query(default="", max_length=120),
    include_broker_audit: bool = Query(default=True),
    limit: int = Query(default=200, ge=1, le=1000),
):
    eng = _paper_engine(request)
    extras = _collect_extra_structured_events(
        request,
        include_broker_audit=bool(include_broker_audit) if isinstance(include_broker_audit, bool) else True,
    )
    return eng.structured_logs(
        window=window if isinstance(window, str) else "all",
        since=since if isinstance(since, str) else "",
        until=until if isinstance(until, str) else "",
        market=market if isinstance(market, str) else "",
        symbol=symbol if isinstance(symbol, str) else "",
        category=category if isinstance(category, str) else "",
        event_type=event_type if isinstance(event_type, str) else "",
        severity=severity if isinstance(severity, str) else "",
        strategy=strategy if isinstance(strategy, str) else "",
        session_id=session_id if isinstance(session_id, str) else "",
        query=query if isinstance(query, str) else "",
        limit=limit if isinstance(limit, int) else 200,
        extra_events=extras,
    )


@router.get("/paper/decisions")
def paper_decisions_inspector(
    request: Request,
    market: str = Query(default="ALL", max_length=16),
    symbol: str = Query(default="", max_length=64),
    strategy: str = Query(default="", max_length=64),
    session_id: str = Query(default="", max_length=80),
    limit: int = Query(default=100, ge=1, le=500),
):
    eng = _paper_engine(request)
    mkt_f = (market if isinstance(market, str) else "ALL").strip().upper() or "ALL"
    sym_f = (symbol if isinstance(symbol, str) else "").strip().upper()
    strat_f = (strategy if isinstance(strategy, str) else "").strip().lower()
    sess_f = (session_id if isinstance(session_id, str) else "").strip()
    lim = limit if isinstance(limit, int) else 100

    items: list[dict[str, Any]] = []
    for mkt_code in ("NSE", "NASDAQ"):
        if mkt_f not in {"ALL", "", mkt_code}:
            continue
        snap = eng.market_snapshot(mkt_code)
        for d in snap.get("decisions") or []:
            row = {
                **d,
                "execution_origin": "LIVE_MARKET_PAPER",
                "simulated": True,
                "execution_badge": "SIMULATED · LIVE_MARKET_PAPER (0 BROKER ORDERS)",
            }
            items.append(row)

    sim = getattr(request.app.state, "simulator", None)
    if sim is not None and mkt_f in {"ALL", "", "LOCAL_SIM", "NASDAQ", "NSE"}:
        for sym_k, kd in (getattr(sim, "kronos_decisions", None) or {}).items():
            items.append(
                {
                    "decision_id": kd.get("decision_id"),
                    "market": sim._infer_kronos_market(sym_k) if hasattr(sim, "_infer_kronos_market") else "NASDAQ",
                    "session_run_id": str((getattr(sim, "run", None) or {}).get("run_id") or ""),
                    "symbol": sym_k,
                    "strategy": kd.get("strategy"),
                    "timeframe": getattr(sim.broker, "timeframe", ""),
                    "action": kd.get("final_signal") or kd.get("action"),
                    "reason": kd.get("reason"),
                    "kronos_status": kd.get("kronos_status"),
                    "forecast_produced": bool(kd.get("forecast_produced")),
                    "forecast_consumed": bool(kd.get("forecast_consumed")),
                    "policy_applied": kd.get("policy_applied"),
                    "explanation": kd,
                    "ts": kd.get("timestamp"),
                    "execution_origin": "LOCAL_SIMULATOR_PAPER",
                    "simulated": True,
                    "execution_badge": "SIMULATED · LOCAL_SIMULATOR_PAPER (0 BROKER ORDERS)",
                }
            )

    filtered: list[dict[str, Any]] = []
    for item in items:
        if mkt_f not in {"ALL", ""} and str(item.get("market") or "").upper() != mkt_f:
            continue
        if sym_f and sym_f not in str(item.get("symbol") or "").upper() and sym_f not in str(item.get("instrument_key") or "").upper():
            continue
        if strat_f and strat_f not in str(item.get("strategy") or "").lower():
            continue
        if sess_f and str(item.get("session_run_id") or "") != sess_f:
            continue
        filtered.append(item)

    filtered.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in filtered:
        key = f"{row.get('market')}:{row.get('symbol')}"
        if key not in by_symbol:
            by_symbol[key] = row

    k_prov = getattr(request.app.state, "kronos_provider", None)
    return {
        "total": len(filtered),
        "decisions": filtered[:lim],
        "latest_by_symbol": list(by_symbol.values()),
        "kronos": k_prov.status() if k_prov is not None else None,
    }


@router.get("/paper/runner")
def paper_runner_status(request: Request):
    runner = getattr(request.app.state, "mm_paper_runner", None)
    eng = _paper_engine(request)
    return {
        "ok": True,
        "selected_markets": eng.selected_markets(),
        "runner": runner.status() if runner is not None else {
            "enabled": False,
            "background_loop_running": False,
            "selected_markets": eng.selected_markets(),
            "live_trading_enabled": False,
            "broker_order_api_calls": 0,
            "markets": {},
        },
    }


@router.get("/paper/selection")
def paper_get_selection(request: Request):
    eng = _paper_engine(request)
    return {"ok": True, "selected_markets": eng.selected_markets()}


@router.post("/paper/selection")
def paper_set_selection(body: MultiMarketSelectionBody, request: Request):
    try:
        return _paper_engine(request).set_selected_markets(body.markets)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/sessions/start")
def paper_multi_sessions_start(body: MultiMarketSessionStartBody, request: Request):
    """Concurrently start and immediately analyze all selected markets in local paper mode."""
    from concurrent.futures import ThreadPoolExecutor

    eng = _paper_engine(request)
    if body.markets:
        try:
            eng.set_selected_markets(body.markets)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    targets = eng.selected_markets()

    def _start_one(mkt: str) -> tuple[str, dict[str, Any]]:
        strat = (body.strategy_by_market.get(mkt) or body.default_strategy or "parallel_ensemble").strip()
        tf = (body.timeframe_by_market.get(mkt) or body.default_timeframe or "5m").strip()
        k_en = body.kronos_enabled_by_market.get(mkt)
        k_pol = body.kronos_policy_by_market.get(mkt)
        snap = eng.start_session(
            mkt,
            strategy=strat,
            timeframe=tf,
            kronos_enabled=k_en,
            kronos_failure_policy=k_pol,
            analyze_immediately=True,
            execute_immediately=body.execute_immediately,
        )
        return mkt, snap

    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futs = {m: pool.submit(_start_one, m) for m in targets}
        for m, fut in futs.items():
            try:
                _, snap_m = fut.result()
                out[m] = snap_m
            except Exception as exc:
                errors[m] = str(exc)[:220]
    if errors and not out:
        raise HTTPException(409, "; ".join(f"{k}: {v}" for k, v in errors.items()))
    return {
        "ok": not errors,
        "simulated_only": True,
        "live_trading_enabled": False,
        "broker_order_api_calls": 0,
        "selected_markets": targets,
        "markets": out,
        "errors": errors,
    }


@router.post("/paper/sessions/transition")
def paper_multi_sessions_transition(body: MultiMarketTransitionBody, request: Request):
    """Concurrently start/pause/resume/stop selected markets; Resume/Start immediately analyzes symbols."""
    from concurrent.futures import ThreadPoolExecutor

    eng = _paper_engine(request)
    targets = [m.strip().upper() for m in body.markets] if body.markets else eng.selected_markets()
    if not body.markets:
        targets = [m for m in targets if eng.execution_mode_provider is None or eng.execution_mode_provider(m) != "LIVE"]
    else:
        for mkt in targets:
            _ensure_paper_routing(request, mkt)
    immediate = body.action.strip().lower() in {"start", "resume"}

    def _trans_one(mkt: str) -> tuple[str, dict[str, Any]]:
        snap = eng.transition_session(
            mkt,
            body.action,
            reason=body.reason,
            analyze_immediately=immediate,
            execute_immediately=bool(immediate and body.execute_immediately),
        )
        return mkt, snap

    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futs = {m: pool.submit(_trans_one, m) for m in targets}
        for m, fut in futs.items():
            try:
                _, snap_m = fut.result()
                out[m] = snap_m
            except Exception as exc:
                errors[m] = str(exc)[:220]
    if errors and not out:
        raise HTTPException(409, "; ".join(f"{k}: {v}" for k, v in errors.items()))
    return {
        "ok": not errors,
        "action": body.action,
        "simulated_only": True,
        "live_trading_enabled": False,
        "broker_order_api_calls": 0,
        "selected_markets": targets,
        "markets": out,
        "errors": errors,
    }


@router.post("/paper/analyze")
def paper_multi_analyze(body: MultiMarketAnalyzeBody, request: Request):
    """Concurrently analyze all selected symbols across all enabled strategies for selected markets (0 orders)."""
    from concurrent.futures import ThreadPoolExecutor

    eng = _paper_engine(request)
    targets = [m.strip().upper() for m in body.markets] if body.markets else eng.selected_markets()
    if not body.markets:
        targets = [m for m in targets if eng.execution_mode_provider is None or eng.execution_mode_provider(m) != "LIVE"]
    else:
        for mkt in targets:
            _ensure_paper_routing(request, mkt)
    reports: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futs = {m: pool.submit(eng.analyze_market, m, trigger="operator_analyze") for m in targets}
        for m, fut in futs.items():
            try:
                reports[m] = fut.result()
            except Exception as exc:
                errors[m] = str(exc)[:220]
    return {
        "ok": not errors,
        "simulated_only": True,
        "live_trading_enabled": False,
        "broker_order_api_calls": 0,
        "selected_markets": targets,
        "reports": reports,
        "errors": errors,
    }


@router.post("/paper/run-cycle")
def paper_multi_run_cycle(body: MultiMarketRunCycleBody, request: Request):
    """Advance all eligible selected markets concurrently with per-market timeout isolation."""
    eng = _paper_engine(request)
    for mkt in body.markets or ():
        _ensure_paper_routing(request, mkt)
    try:
        return eng.advance_selected_markets_concurrently(
            markets=body.markets,
            only_active=body.only_active,
            market_timeout_seconds=body.market_timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/paper/{market}")
def paper_snapshot(market: str, request: Request):
    try:
        return _paper_engine(request).market_snapshot(market)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/session/start")
def paper_session_start(body: PaperSessionStartBody, request: Request):
    _ensure_paper_routing(request, body.market)
    try:
        return _paper_engine(request).start_session(
            body.market,
            strategy=body.strategy,
            timeframe=body.timeframe,
            symbols=body.symbols,
            kronos_enabled=body.kronos_enabled,
            kronos_failure_policy=body.kronos_failure_policy,
            analyze_immediately=True,
            execute_immediately=True,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/paper/session/transition")
def paper_session_transition(body: PaperSessionTransitionBody, request: Request):
    _ensure_paper_routing(request, body.market)
    immediate = body.action.strip().lower() in {"start", "resume"}
    try:
        return _paper_engine(request).transition_session(
            body.market,
            body.action,
            reason=body.reason,
            analyze_immediately=immediate,
            execute_immediately=immediate,
        )
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
    _ensure_paper_routing(request, body.market)
    updates = body.model_dump(exclude={"market"}, exclude_none=True)
    try:
        return {"ok": True, "config": _paper_engine(request).update_config(body.market, **updates)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/paper/preview")
def paper_preview(body: PaperOrderBody, request: Request):
    _ensure_paper_routing(request, body.market)
    target_sym = (body.instrument_key or body.symbol) if body.market.upper() == "NSE" else body.symbol
    return _paper_engine(request).preview_order(
        market=body.market,
        symbol=target_sym,
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
    _ensure_paper_routing(request, body.market)
    target_sym = (body.instrument_key or body.symbol) if body.market.upper() == "NSE" else body.symbol
    return _paper_engine(request).submit_order(
        client_order_id=body.client_order_id,
        market=body.market,
        symbol=target_sym,
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
    target_sym = (body.instrument_key or body.symbol) if body.market.upper() == "NSE" else body.symbol
    try:
        return _paper_engine(request).modify_position_protection(
            body.market,
            target_sym,
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


@router.get("/paper-modes")
def paper_modes_summary(request: Request):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "modes": _build_paper_modes_catalog(request),
    }


@router.get("/paper/audit/events")
def paper_audit(request: Request, market: str = "", limit: int = Query(default=150, ge=1, le=500)):
    return {"events": _paper_engine(request).audit_log(market=market, limit=limit)}


@router.get("/paper/audit/export")
def paper_audit_export(request: Request, market: str = "", limit: int = Query(default=500, ge=1, le=2000)):
    try:
        return _paper_engine(request).export_audit_bundle(market=market, limit=limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# -- Live Status & Atomic Global Emergency Stop --------------------------------


@router.get("/live/status")
def live_status(request: Request):
    summary = _live_status_summary(request)
    coordinator = getattr(request.app.state, "live_execution", None)
    runner = getattr(request.app.state, "mm_live_runner", None)
    summary["execution"] = coordinator.live_status_summary() if coordinator is not None else None
    summary["runner"] = runner.status() if runner is not None else None
    summary["audit"] = coordinator.journal.audit_log(limit=40) if coordinator is not None else []
    return summary


@router.post("/emergency-stop")
def global_emergency_stop(body: GlobalEmergencyBody, request: Request):
    """Atomic Emergency Stop across Local Paper, Upstox Autonomy, Alpaca Broker, and legacy state."""
    reason = body.reason or "operator global emergency stop"
    results: dict[str, Any] = {}

    eng = getattr(request.app.state, "live_paper_engine", None)
    if eng is not None:
        results["live_market_paper"] = eng.emergency_stop(reason)

    k_prov = getattr(request.app.state, "kronos_provider", None)
    if k_prov is not None:
        results["kronos"] = k_prov.stop_all_sessions(reason=reason)

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


@router.get("/start-monitor")
def start_monitor_overview(request: Request):
    """One read-only projection for the single-page Start & Monitor screen.

    Composes state that already exists (paper engine, live coordinator, scan
    capacity, credential presence, readiness receipts). It cannot place, modify
    or cancel anything: the only writes the screen performs go through the
    existing per-market session and live-gate routes.
    """
    from app.markets.start_monitor import build_launchpad

    eng = getattr(request.app.state, "live_paper_engine", None)
    coord = getattr(request.app.state, "live_execution", None)
    scan = getattr(request.app.state, "scan_capacity_service", None)

    watchlists: dict[str, list[dict[str, Any]]] = {"NSE": [], "NASDAQ": []}
    if eng is not None:
        try:
            watchlists = {**watchlists, **(eng.list_watchlists() or {})}
        except (KeyError, ValueError, OSError):
            watchlists = {"NSE": [], "NASDAQ": []}

    data_credentials: dict[str, dict[str, Any]] = {}
    upstox_tok, upstox_src = _upstox_readonly_token(request)
    data_credentials["NSE"] = {"present": bool(upstox_tok), "source": upstox_src}
    alpaca_creds, alpaca_src = _alpaca_readonly_creds(request)
    data_credentials["NASDAQ"] = {
        "present": bool(alpaca_creds.get("api_key") and alpaca_creds.get("api_secret")),
        "source": alpaca_src,
        "feed": alpaca_creds.get("feed"),
    }

    broker_rt = getattr(request.app.state, "alpaca_broker_runtime", None)
    try:
        broker_credentials = broker_rt.credential_status() if broker_rt is not None else None
    except (RuntimeError, ValueError, KeyError):
        broker_credentials = None
    if broker_credentials is not None:
        # Presence only: drop anything that is not a boolean/slot label.
        broker_credentials = {
            "paper": {"present": bool((broker_credentials.get("paper") or {}).get("present"))},
            "live": {"present": bool((broker_credentials.get("live") or {}).get("present"))},
        }
    live_tok, _live_src = resolve_live_token(getattr(request.app.state, "upstox_live_token", None))

    try:
        readiness = _build_readiness_matrix(request)
    except (KeyError, ValueError, RuntimeError):
        readiness = None

    from app.strategies import catalog as strategy_catalog

    return build_launchpad(
        engine=eng,
        coordinator=coord,
        scan_service=scan,
        watchlists=watchlists,
        data_credentials=data_credentials,
        broker_credentials=broker_credentials,
        upstox_live_token_present=bool(live_tok),
        readiness_matrix=readiness,
        strategies=strategy_catalog(),
    )


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
    instrument_key: str = Query(default="", max_length=64),
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
        instrument_key=instrument_key,
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


# ---------------------------------------------------------------------------
# LIVE execution (separate real-money path; per-market gate, disarmed by default)
# ---------------------------------------------------------------------------


class LiveModeBody(BaseModel):
    mode: str = Field(pattern="^(OFF|PAPER|LIVE|off|paper|live)$")


class LiveActivateBody(BaseModel):
    confirm_live_trading: str = Field(default="", max_length=120)
    acknowledge_risk: bool = False


class LiveDisarmBody(BaseModel):
    reason: str = Field(default="operator disarm", max_length=200)


class LiveEmergencyStopBody(BaseModel):
    reason: str = Field(default="operator emergency stop", max_length=200)
    cancel_working: bool = False


class LiveResetKillBody(BaseModel):
    operator_ack: bool = False


class LiveLimitsBody(BaseModel):
    max_order_value: float | None = Field(default=None, gt=0)
    max_daily_orders: int | None = Field(default=None, gt=0)
    max_daily_loss: float | None = Field(default=None, gt=0)
    max_open_positions: int | None = Field(default=None, gt=0)
    max_symbol_exposure: float | None = Field(default=None, gt=0)
    max_total_exposure: float | None = Field(default=None, gt=0)
    max_quote_age_seconds: float | None = Field(default=None, gt=0)
    max_price_deviation_bps: float | None = Field(default=None, gt=0)


def _live_http_error(exc: Exception) -> HTTPException:
    from app.markets.live_execution import (
        LiveBrokerRejected,
        LiveExecutionError,
        LiveGateRefused,
        LiveReconciliationFailed,
        LiveSubmissionUncertain,
    )

    if isinstance(exc, LiveGateRefused):
        return HTTPException(409, str(exc))
    if isinstance(exc, LiveReconciliationFailed):
        return HTTPException(503, str(exc))
    if isinstance(exc, LiveSubmissionUncertain):
        return HTTPException(503, str(exc))
    if isinstance(exc, LiveBrokerRejected):
        return HTTPException(422, str(exc))
    if isinstance(exc, LiveExecutionError):
        return HTTPException(422, str(exc))
    return HTTPException(500, "Live execution request failed")


@router.get("/live/audit")
def live_execution_audit(
    request: Request,
    market: str = Query(default="", max_length=8),
    limit: int = Query(default=150, ge=1, le=500),
):
    coordinator = _live_coordinator(request)
    mkt = market.upper() if market.upper() in {"NSE", "NASDAQ"} else ""
    return {
        "ok": True,
        "real_money": True,
        "events": coordinator.journal.audit_log(market=mkt, limit=limit),
    }


@router.get("/live/readiness")
def live_readiness_report(request: Request):
    """One operator readiness report for both markets (no broker contact, no secrets)."""
    from app.markets.live_readiness import build_readiness_report

    coordinator = _live_coordinator(request)
    try:
        return build_readiness_report(coordinator)
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.get("/live/{market}/readiness")
def live_readiness(market: str, request: Request, symbol: str | None = Query(default=None)):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.readiness(market, symbol=symbol)
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/mode")
def live_set_mode(market: str, body: LiveModeBody, request: Request):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.set_mode(market, body.mode)
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/limits")
def live_set_limits(market: str, body: LiveLimitsBody, request: Request):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.update_limits(market, **body.model_dump(exclude_none=True))
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/arm")
def live_arm(market: str, request: Request, symbol: str | None = Query(default=None)):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.arm(market, symbol=symbol)
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/activate")
def live_activate(market: str, body: LiveActivateBody, request: Request):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.activate(
            market,
            confirm_phrase=body.confirm_live_trading,
            acknowledge_risk=body.acknowledge_risk,
        )
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/disarm")
def live_disarm(market: str, body: LiveDisarmBody, request: Request):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.disarm(market, body.reason)
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/emergency-stop")
def live_emergency_stop(market: str, body: LiveEmergencyStopBody, request: Request):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.emergency_stop(
            market, body.reason, cancel_working=body.cancel_working
        )
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/reset-kill-switch")
def live_reset_kill_switch(market: str, body: LiveResetKillBody, request: Request):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.reset_kill_switch(market, operator_ack=body.operator_ack)
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/reconcile")
def live_reconcile(market: str, request: Request):
    coordinator = _live_coordinator(request)
    try:
        return coordinator.reconcile(market)
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc


@router.post("/live/{market}/run-cycle")
def live_run_cycle(market: str, request: Request):
    """Evaluate all selected symbols and route only ACTIVE-gate live decisions."""
    coordinator = _live_coordinator(request)
    try:
        return coordinator.advance_market(
            market, getattr(request.app.state, "ensemble_service", None)
        )
    except (ValueError, RuntimeError) as exc:
        raise _live_http_error(exc) from exc
