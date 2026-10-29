"""Operator connection workflow: read-only provider tests and market overview.

One route per market lets the operator verify credentials/entitlements before
starting a local PAPER session. Market-data only: no order route exists here,
and responses carry verdicts/guidance rather than credentials.
"""

from fastapi import APIRouter, Query, Request

from app.data.alpaca import feed_label
from app.market_data.connection_probe import (
    probe_alpaca_connection,
    probe_upstox_connection,
)

router = APIRouter(prefix="/api/connections")


@router.get("/")
def overview(request: Request):
    settings = request.app.state.settings
    upstox_configured = bool(str(getattr(settings, "market_data_access_token", "") or "").strip())
    alpaca_configured = bool(
        str(getattr(settings, "alpaca_api_key", "") or "").strip()
        and str(getattr(settings, "alpaca_api_secret", "") or "").strip()
    )
    return {
        "markets": [
            {
                "market": "india",
                "provider": "upstox",
                "currency": "INR",
                "calendar": "nse-session (IST, exchange market-info events)",
                "data_source_selected": str(settings.data_source).lower() == "upstox",
                "credential_present": upstox_configured,
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
                "instruments_configured": list(settings.alpaca_symbol_list()),
                "feed": settings.alpaca_data_feed,
                "feed_label": feed_label(settings.alpaca_data_feed),
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
            "read from or written to these endpoints."
        ),
    }


@router.post("/upstox/test")
async def test_upstox(request: Request):
    """Read-only Upstox authorize probe for the configured token."""
    return await probe_upstox_connection(request.app.state.settings)


@router.post("/alpaca/test")
def test_alpaca(
    request: Request,
    symbol: str = Query(default="", max_length=20),
):
    """Read-only Alpaca latest-bar + listing-metadata probe for a ticker."""
    return probe_alpaca_connection(
        request.app.state.settings, symbol=symbol or None,
    )
