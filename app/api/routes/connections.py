"""Operator connection workflow: read-only provider tests, DPAPI data keys, and role matrix.

Market-data only: no order route exists here, and responses carry verdicts,
freshness, timestamps, and guidance rather than credentials.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.autonomy.upstox_token import resolve_token
from app.data.alpaca import feed_label
from app.market_data.connection_probe import (
    probe_alpaca_connection,
    probe_upstox_connection,
)
from app.market_data.provider_verification import (
    DEFAULT_NASDAQ_TEST_SYMBOL,
    DEFAULT_NSE_TEST_KEY,
    build_provider_matrix,
    verify_alpaca_market_data,
    verify_upstox_market_data,
)
from app.markets.alpaca_credentials import resolve_alpaca_data_credentials

router = APIRouter(prefix="/api/connections")


class AlpacaDataCredentialBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=256)
    api_secret: str = Field(min_length=1, max_length=256)
    feed: str = Field(default="iex", pattern="^(iex|sip|delayed_sip)$")


def _resolve_upstox_readonly(request: Request) -> tuple[str, str]:
    tok, src = resolve_token(getattr(request.app.state, "upstox_token", None))
    if tok:
        return tok, src
    settings = request.app.state.settings
    env_tok = str(getattr(settings, "market_data_access_token", "") or "").strip()
    if env_tok:
        return env_tok, "environment"
    return "", "none"


def _resolve_alpaca_readonly(request: Request) -> tuple[dict[str, str], str]:
    return resolve_alpaca_data_credentials(
        getattr(request.app.state, "alpaca_data_store", None),
        request.app.state.settings,
    )


@router.get("/")
def overview(request: Request):
    settings = request.app.state.settings
    upstox_tok, upstox_src = _resolve_upstox_readonly(request)
    alpaca_creds, alpaca_src = _resolve_alpaca_readonly(request)
    alpaca_configured = bool(alpaca_creds["api_key"] and alpaca_creds["api_secret"])
    return {
        "markets": [
            {
                "market": "india",
                "provider": "upstox",
                "currency": "INR",
                "calendar": "nse-session (IST, exchange market-info events)",
                "data_source_selected": str(settings.data_source).lower() == "upstox",
                "credential_present": bool(upstox_tok),
                "credential_source": upstox_src,
                "instruments_configured": list(settings.market_data_symbol_list()),
                "feed_label": "UPSTOX MARKET DATA FEED V3 · READ-ONLY · LOCAL PAPER FILLS",
            },
            {
                "market": "us",
                "provider": "alpaca",
                "currency": "USD",
                "calendar": "us-equities (America/New_York, NYSE rules)",
                "data_source_selected": str(settings.data_source).lower() == "alpaca",
                "credential_present": alpaca_configured,
                "credential_source": alpaca_src,
                "instruments_configured": list(settings.alpaca_symbol_list()),
                "feed": alpaca_creds["feed"],
                "feed_label": feed_label(alpaca_creds["feed"]),
            },
        ],
        "execution_mode": "AXIOM_PAPER",
        "live_trading": False,
        "separation": (
            "India and U.S. instruments, currencies, calendars, credentials and "
            "local PAPER portfolios are kept separate; a combined view appears "
            "only when both markets are explicitly selected."
        ),
        "note": (
            "Credential presence is reported as a boolean only; secrets are never "
            "returned from these endpoints."
        ),
    }


@router.get("/matrix")
def matrix(request: Request):
    return build_provider_matrix(request.app.state)


@router.get("/alpaca/credentials")
def alpaca_credentials_status(request: Request):
    store = getattr(request.app.state, "alpaca_data_store", None)
    creds, source = _resolve_alpaca_readonly(request)
    base = store.status() if store is not None else {
        "slot": "ALPACA_MARKET_DATA",
        "present": False,
        "feed": None,
        "saved_at": None,
        "backend": "unavailable",
        "secret_returned": False,
    }
    return {
        **base,
        "credential_present": bool(creds["api_key"] and creds["api_secret"]),
        "source": source,
        "effective_feed": creds["feed"],
        "feed_label": feed_label(creds["feed"]),
        "execution_effect": "NONE_MARKET_DATA_STORAGE_ONLY",
    }


def _receipts(request: Request) -> dict[str, dict]:
    rec = getattr(request.app.state, "owner_verification_receipts", None)
    if not isinstance(rec, dict):
        rec = {}
        request.app.state.owner_verification_receipts = rec
    return rec


@router.post("/alpaca/credentials")
def alpaca_credentials_save(body: AlpacaDataCredentialBody, request: Request):
    store = getattr(request.app.state, "alpaca_data_store", None)
    if store is None:
        raise HTTPException(503, "Protected Alpaca market-data store is unavailable on this host")
    try:
        saved = store.save(body.api_key, body.api_secret, body.feed)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _receipts(request).pop("ALPACA_MARKET_DATA", None)
    return {
        "saved": True,
        **saved,
        "source": "protected-store",
        "feed_label": feed_label(body.feed),
        "execution_effect": "NONE_MARKET_DATA_STORAGE_ONLY",
    }


@router.delete("/alpaca/credentials")
def alpaca_credentials_delete(request: Request):
    store = getattr(request.app.state, "alpaca_data_store", None)
    if store is None:
        raise HTTPException(503, "Protected Alpaca market-data store is unavailable on this host")
    deleted = store.delete()
    _receipts(request).pop("ALPACA_MARKET_DATA", None)
    return {
        "deleted": deleted,
        **store.status(),
        "execution_effect": "NONE_MARKET_DATA_STORAGE_ONLY",
    }


@router.post("/upstox/test")
async def test_upstox(request: Request):
    """Read-only Upstox authorize probe for the configured DPAPI or env token."""
    tok, src = _resolve_upstox_readonly(request)
    return await probe_upstox_connection(
        request.app.state.settings,
        token_override=tok,
        credential_source=src,
    )


@router.post("/upstox/verify-quote")
def verify_upstox_quote(
    request: Request,
    instrument_key: str = Query(default=DEFAULT_NSE_TEST_KEY, max_length=64),
):
    """Harmless read-only Upstox profile + market-quote verification."""
    tok, src = _resolve_upstox_readonly(request)
    res = verify_upstox_market_data(
        tok,
        credential_source=src,
        instrument_key=instrument_key or DEFAULT_NSE_TEST_KEY,
    )
    if res.get("authenticated"):
        _receipts(request)["UPSTOX_MARKET_DATA"] = {
            "verified": True,
            "connection_state": res.get("connection_state"),
            "verified_at": res.get("checked_at"),
            "instrument_key": instrument_key or DEFAULT_NSE_TEST_KEY,
        }
    else:
        _receipts(request).pop("UPSTOX_MARKET_DATA", None)
    return res


@router.post("/alpaca/test")
def test_alpaca(
    request: Request,
    symbol: str = Query(default="", max_length=20),
):
    """Read-only Alpaca latest-bar + listing-metadata probe for a ticker."""
    creds, src = _resolve_alpaca_readonly(request)
    return probe_alpaca_connection(
        request.app.state.settings,
        symbol=symbol or DEFAULT_NASDAQ_TEST_SYMBOL,
        credentials_override=creds,
        credential_source=src,
    )


@router.post("/alpaca/verify-quote")
def verify_alpaca_quote(
    request: Request,
    symbol: str = Query(default=DEFAULT_NASDAQ_TEST_SYMBOL, max_length=20),
):
    """Harmless read-only Alpaca snapshot + listing verification."""
    creds, src = _resolve_alpaca_readonly(request)
    res = verify_alpaca_market_data(
        creds["api_key"],
        creds["api_secret"],
        feed=creds["feed"],
        credential_source=src,
        symbol=symbol or DEFAULT_NASDAQ_TEST_SYMBOL,
    )
    if res.get("authenticated"):
        _receipts(request)["ALPACA_MARKET_DATA"] = {
            "verified": True,
            "connection_state": res.get("connection_state"),
            "verified_at": res.get("checked_at"),
            "symbol": symbol or DEFAULT_NASDAQ_TEST_SYMBOL,
            "feed": creds["feed"],
        }
    else:
        _receipts(request).pop("ALPACA_MARKET_DATA", None)
    return res
