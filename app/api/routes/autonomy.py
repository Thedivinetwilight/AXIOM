"""Authenticated autonomy API and local operator page.

There is deliberately no order-confirm endpoint.  The only credential request
model is the manual Upstox access-token entry: the token value travels in a
POST body (never a URL), is stored encrypted for the current Windows user, is
never returned/displayed/logged, and is used only for Upstox market data and
read-only account checks.  Daily OAuth remains available as the environment
token source.  Saving or testing a token never creates a session, changes
execution mode, Arms/Activates, or starts automation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import authenticate, loopback_or_bearer
from app.autonomy.kronos_artifacts import ArtifactError
from app.autonomy.service import NotReady, SessionError
from app.autonomy.upstox import (
    InvalidQuote, NoTradedQuote, Quote, ReconciliationFailure, UpstoxAuthError,
    UpstoxError,
)
from app.autonomy.upstox_token import resolve_live_token, resolve_token

api = APIRouter(prefix="/api/autonomy", dependencies=[Depends(authenticate)])
page = APIRouter()


class CreateSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["PAPER", "UPSTOX_LIVE"]
    instrument_keys: list[str] = Field(min_length=1, max_length=500)


class RiskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_daily_loss: float = Field(gt=0)
    per_trade_risk_fraction: float = Field(gt=0, le=0.01)
    equity_at_entry: float | None = Field(default=None, gt=0)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_key: str = Field(min_length=3, max_length=160)


class KronosPythonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executable: str = Field(default="", max_length=1024)


class UpstoxTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The token travels only in this POST body: never a URL, never a query.
    access_token: str = Field(min_length=1, max_length=4096)


class UpstoxTokenTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_key: str = Field(min_length=3, max_length=160)


class ActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # LIVE activation requires this exact confirmation plus the strategy
    # acknowledgement. PAPER activation carries neither.
    confirm_live_trading: str = Field(default="", max_length=64)
    strategy_acknowledged: bool = False


def service(request: Request):
    value = getattr(request.app.state, "autonomy", None)
    if value is None:
        detail = getattr(request.app.state, "autonomy_error", "Autonomy service unavailable")
        raise HTTPException(503, detail)
    return value


def token_store(request: Request):
    store = getattr(request.app.state, "upstox_token", None)
    if store is None:
        raise HTTPException(503, "The protected credential store is unavailable on this host")
    return store


def call(request: Request, operation):
    try:
        return operation(service(request))
    except NotReady as exc:
        raise HTTPException(409, str(exc)) from None
    except SessionError as exc:
        raise HTTPException(409, str(exc)) from None
    except UpstoxAuthError as exc:
        raise HTTPException(503, str(exc)) from None
    except ReconciliationFailure as exc:
        raise HTTPException(503, str(exc)) from None
    except UpstoxError as exc:
        raise HTTPException(502, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None
    except ArtifactError as exc:
        raise HTTPException(409, str(exc)) from None


@api.get("/status")
def status(request: Request):
    value = service(request)
    return {
        "sessions": value.sessions(),
        "kronos": value.kronos_view(),
        "catalog": {
            "refreshed_at": value.catalog.refreshed_at.isoformat() if value.catalog.refreshed_at else None,
            "source_sha256": value.catalog.source_sha256,
            "count": len(value.catalog.instruments),
            "warnings": value.catalog.warnings,
            "stats": value.catalog.stats,
        },
        "credential_api": "none; use local OAuth then POST authorize without a token body",
        "automatic_arm_or_activate": False,
        "profitability_claim": "none",
    }


@api.get("/kronos")
def kronos_status(request: Request):
    # Read-only setup/artifact/runtime view; no account or execution state.
    return service(request).kronos_view()


@api.post("/kronos/python")
def kronos_python(body: KronosPythonBody, request: Request):
    # One validated executable path only; no arguments, packages or credentials.
    return call(request, lambda value: value.kronos_python(body.executable))


@api.post("/kronos/install", status_code=202)
def kronos_install(request: Request):
    # The request has no URL, repository, revision, package or credential body.
    return call(request, lambda value: value.kronos_install())


@api.post("/kronos/self-test", status_code=202)
def kronos_self_test(request: Request):
    # Offline verification and real inference only; no synthetic forecast fallback.
    return call(request, lambda value: value.kronos_self_test())


@api.post("/kronos/cancel")
def kronos_cancel(request: Request):
    return call(request, lambda value: value.kronos_cancel())


@api.get("/catalog")
def catalog(request: Request, query: str = "", limit: int = 100):
    value = service(request)
    limit = max(1, min(limit, 500))
    from app.autonomy.upstox import _clean_identity

    needle = _clean_identity(query).upper()
    # Exact-key alias: instrument-file variants drift the equity key suffix
    # away from the ISIN, while the documented identity stays ISIN-based.
    # A needle of the form SEGMENT|<ISIN> must resolve to the row whose ISIN
    # matches even when the stored key drifted.
    alias_isin = ""
    alias_segment = ""
    if "|" in needle:
        alias_segment, alias_tail = needle.split("|", 1)
        if alias_tail.startswith("IN") and 10 <= len(alias_tail) <= 12:
            alias_isin = alias_tail

    def matches(item) -> bool:
        if not needle:
            return True
        haystack = " ".join(
            _clean_identity(str(value)).upper()
            for value in (item.instrument_key, item.trading_symbol,
                          item.underlying_symbol, item.name, item.isin,
                          item.short_name if hasattr(item, "short_name") else "")
        )
        if needle in haystack:
            return True
        return bool(alias_isin and item.segment.upper() == alias_segment
                    and _clean_identity(str(item.isin)).upper() == alias_isin)

    items = [item.public() for item in value.catalog.instruments.values() if matches(item)]
    items.sort(key=lambda row: str(row.get("instrument_key") or ""))
    return {
        "scope": "NSE/BSE equities and NCD/BCD exchange-listed currency derivatives; no spot forex",
        "refreshed_at": value.catalog.refreshed_at.isoformat() if value.catalog.refreshed_at else None,
        "source_sha256": value.catalog.source_sha256,
        "total": len(value.catalog.instruments), "returned": len(items[:limit]),
        "warnings": value.catalog.warnings, "instruments": items[:limit],
        # The UI's diagnostics line renders these; omitting them silently hid
        # every parse drop from the operator (regression-pinned in tests).
        "stats": value.catalog.stats,
    }


@api.post("/catalog/refresh")
def refresh_catalog(request: Request):
    # Data-only operation: does not create a session, change mode, Arm,
    # Activate, or issue an execution permit.
    return call(request, lambda value: value.refresh_catalog())


# --------------------------------------------------------------------------
# Manual Upstox access-token entry (market data + read-only account checks).
# The token value never appears in a response, a URL, a log line or an error.


@api.get("/upstox-token/status")
def upstox_token_status(request: Request):
    store = token_store(request)
    status = dict(store.status())
    _, source = resolve_token(store)
    status["source"] = source
    status["execution_effect"] = "NONE_TOKEN_STORAGE_ONLY"
    return status


@api.post("/upstox-token")
def upstox_token_save(body: UpstoxTokenBody, request: Request):
    store = token_store(request)
    replaced = bool(store.status()["present"])
    try:
        status = store.save(body.access_token)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(503, "The protected credential store is unavailable; nothing was stored") from None
    return {"saved": True, "replaced": replaced,
            "execution_effect": "NONE_TOKEN_STORAGE_ONLY", **status}


@api.delete("/upstox-token")
def upstox_token_delete(request: Request):
    store = token_store(request)
    deleted = store.delete()
    return {"deleted": deleted, "present": False, "saved_at": None,
            "token_returned": False, "execution_effect": "NONE_TOKEN_STORAGE_ONLY"}


# --------------------------------------------------------------------------
# Separate LIVE trading credential (real money). Distinct protected slot;
# never silently reused from the read-only market-data token. Save/Delete
# only — authentication and account identity are verified at Authorize/Arm.


def live_token_store(request: Request):
    store = getattr(request.app.state, "upstox_live_token", None)
    if store is None:
        raise HTTPException(503, "The protected credential store is unavailable on this host")
    return store


@api.get("/upstox-live-token/status")
def upstox_live_token_status(request: Request):
    store = live_token_store(request)
    status = dict(store.status())
    _, source = resolve_live_token(store)
    status["source"] = source
    status["label"] = "LIVE TRADING CREDENTIAL · real money · order writes"
    status["execution_effect"] = "NONE_TOKEN_STORAGE_ONLY"
    return status


@api.post("/upstox-live-token")
def upstox_live_token_save(body: UpstoxTokenBody, request: Request):
    store = live_token_store(request)
    replaced = bool(store.status()["present"])
    try:
        status = store.save(body.access_token)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(503, "The protected credential store is unavailable; nothing was stored") from None
    return {"saved": True, "replaced": replaced,
            "label": "LIVE TRADING CREDENTIAL · real money · order writes",
            "execution_effect": "NONE_TOKEN_STORAGE_ONLY", **status}


@api.delete("/upstox-live-token")
def upstox_live_token_delete(request: Request):
    store = live_token_store(request)
    deleted = store.delete()
    return {"deleted": deleted, "present": False, "saved_at": None,
            "token_returned": False, "execution_effect": "NONE_TOKEN_STORAGE_ONLY"}


def _token_test_failure(verdict: str, detail: str, base: dict) -> dict:
    return {"ok": False, "verdict": verdict, "detail": detail, **base}


@api.post("/upstox-token/test")
def upstox_token_test(body: UpstoxTokenTestBody, request: Request):
    """Read-only connection test: authenticate, then fetch ONE exact quote.

    Only GET /v2/user/profile and GET /v2/market-quote/quotes (the documented
    Full Market Quotes endpoint) are ever called. No session is created;
    execution mode, Arm/Activate and automation state are untouched. The
    response carries only the instrument key, quote timestamp, freshness and
    sanitized success/failure details — never prices, never the token.
    """
    value = service(request)
    key = body.instrument_key.strip()
    item = value.catalog.instruments.get(key)
    if item is None:
        raise HTTPException(
            400,
            "Instrument key is not present in the refreshed BOD catalog; "
            "select an exact key from the search results",
        )
    store = token_store(request)
    token, source = resolve_token(store)
    base = {
        "instrument_key": key,
        "quote_timestamp": None,
        "freshness": {"label": "no-quote", "age_seconds": None},
        "authenticated": False,
        "token_source": source,
        "checked_at": value.clock().isoformat(),
        "read_only": True,
        "real_order_api_used": False,
        "execution_mode": "AXIOM_PAPER",
        "session_effect": "NONE",
    }
    if not token:
        return _token_test_failure(
            "missing_token",
            "No Upstox access token is configured. Save a manual token on this "
            "page or run the daily OAuth login; nothing was sent to Upstox.",
            base,
        )
    client = value.reader_factory(value.token_provider)
    try:
        client.profile()
    except UpstoxAuthError:
        return _token_test_failure(
            "rejected_credential",
            "Upstox rejected the credential (HTTP 401/403): it is expired, "
            "invalid or was revoked. Generate a new token and Save it again.",
            base,
        )
    except ReconciliationFailure as exc:
        base["authenticated"] = False
        verdict = "unreachable" if "unreachable" in str(exc) else "read_failed"
        return _token_test_failure(verdict, str(exc), base)
    base["authenticated"] = True
    try:
        quote = client.quote_snapshot(key)
    except UpstoxAuthError:
        return _token_test_failure(
            "rejected_credential",
            "Upstox rejected the credential mid-test (expired, invalid or "
            "revoked). Generate a new token and Save it again.",
            base,
        )
    except (NoTradedQuote, InvalidQuote) as exc:
        # Fail-closed, but honest and precise: the response existed and its
        # observed fields are named. No replay/sample data is substituted.
        if exc.timestamp is not None:
            age = (value.clock() - exc.timestamp).total_seconds()
            base["quote_timestamp"] = exc.timestamp.isoformat()
            base["freshness"] = {"label": "fresh" if 0 <= age <= 90 else "stale",
                                 "age_seconds": int(age)}
        verdict = "no-trade" if isinstance(exc, NoTradedQuote) else "invalid-quote"
        return _token_test_failure(verdict, str(exc), base)
    except ReconciliationFailure as exc:
        verdict = "unreachable" if "unreachable" in str(exc) else "read_failed"
        return _token_test_failure(verdict, str(exc), base)
    age = (value.clock() - quote.timestamp).total_seconds()
    fresh = 0 <= age <= 90
    base["quote_timestamp"] = quote.timestamp.isoformat()
    base["freshness"] = {"label": "fresh" if fresh else "stale",
                         "age_seconds": int(age)}
    if fresh:
        return {
            "ok": True, "verdict": "ok",
            "detail": "Authenticated (read-only profile) and a fresh full quote "
                      "was returned for this exact BOD key. Market data only; "
                      "paper fills remain local.",
            **base,
        }
    return _token_test_failure(
        "stale_quote",
        f"Authenticated, but the quote timestamp is {int(age)} seconds old and "
        "is NOT treated as current. The market may be closed or the feed "
        "delayed; retry when the market is live.",
        base,
    )


@api.post("/sessions", status_code=201)
def create_session(body: CreateSessionBody, request: Request):
    return call(request, lambda value: value.create_session(body.mode, body.instrument_keys))


@api.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request):
    return call(request, lambda value: value.public(session_id))


@api.post("/sessions/{session_id}/recover-view")
def recover_view(session_id: str, request: Request):
    # Browser/session restoration is a local lifecycle stop, never a broker
    # order action. It clears approval and exposes only the durable view.
    return call(request, lambda value: value.recover_view(session_id))


@api.post("/sessions/{session_id}/authorize")
def authorize(session_id: str, request: Request):
    # No body, no credential field, no credential in logs/responses.
    return call(request, lambda value: value.authorize(session_id))


@api.post("/sessions/{session_id}/risk")
def risk(session_id: str, body: RiskBody, request: Request):
    return call(request, lambda value: value.set_risk(
        session_id, max_daily_loss=body.max_daily_loss,
        per_trade_risk_fraction=body.per_trade_risk_fraction,
        equity_at_entry=body.equity_at_entry,
    ))


@api.post("/sessions/{session_id}/arm")
def arm(session_id: str, request: Request):
    return call(request, lambda value: value.arm(session_id))


@api.post("/sessions/{session_id}/activate")
def activate(session_id: str, request: Request, body: ActivateBody | None = None):
    payload = body or ActivateBody()
    return call(request, lambda value: value.activate(
        session_id,
        confirm_live_trading=payload.confirm_live_trading,
        strategy_acknowledged=payload.strategy_acknowledged,
    ))


@api.post("/sessions/{session_id}/pause")
def pause(session_id: str, request: Request):
    return call(request, lambda value: value.pause(session_id))


@api.post("/sessions/{session_id}/emergency-stop")
def emergency_stop(session_id: str, request: Request):
    return call(request, lambda value: value.emergency_stop(session_id))


@api.post("/sessions/{session_id}/scan")
def scan_read_only(session_id: str, request: Request):
    # This route is deliberately bound only to the read-only service method.
    # The worker's order-eligible execute_once method is not exposed by any API.
    return call(request, lambda value: value.scan_once(session_id))


@api.post("/sessions/{session_id}/reconcile")
def reconcile(session_id: str, request: Request):
    return call(request, lambda value: value.reconcile(session_id))


@api.post("/sessions/{session_id}/cancel-unfilled")
def cancel_unfilled(session_id: str, body: CancelBody, request: Request):
    return call(request, lambda value: value.cancel_unfilled(session_id, body.instrument_key))


@api.get("/sessions/{session_id}/audit")
def audit(session_id: str, request: Request, limit: int = 200):
    return call(request, lambda value: value.audit(session_id, limit))


@api.get("/sessions/{session_id}/bars/{instrument_key:path}")
def bars(session_id: str, instrument_key: str, request: Request):
    def operation(value):
        runtime = value._runtime(session_id)
        if instrument_key not in runtime.instruments:
            raise SessionError("Instrument is not selected in this session")
        bars = runtime.bars.get(instrument_key, [])
        return {
            "instrument_key": instrument_key, "source": "UPSTOX_COMPLETED_BARS",
            "display_only": True, "tradingview_data_used": False,
            "bars": [bar.to_dict() for bar in bars],
        }
    return call(request, operation)


@page.get("/autonomy", include_in_schema=False, dependencies=[Depends(loopback_or_bearer)])
def autonomy_page():
    return FileResponse(Path(__file__).resolve().parent.parent.parent / "static" / "autonomy" / "index.html")


@page.get("/autonomy/manual", include_in_schema=False, dependencies=[Depends(loopback_or_bearer)])
def autonomy_manual():
    path = Path(__file__).resolve().parents[3] / "docs" / "AUTONOMY.md"
    if not path.is_file():
        return PlainTextResponse("Autonomy manual is missing from this installation", status_code=500)
    return PlainTextResponse(path.read_text(encoding="utf-8"))
