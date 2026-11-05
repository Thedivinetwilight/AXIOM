"""Read-only provider connection tests for the operator workflow.

Everything here is market DATA verification only. The Upstox side runs the
documented GET authorize probe; the Alpaca side requests one latest-bar read
and (optionally) listing metadata. No order endpoint is referenced anywhere,
no credential is stored, echoed or logged, and every verdict is fail-closed:
an uncertain result is reported as uncertain, never as connected.

These probes verify credentials and entitlements against the real provider
when the host machine has network access and configured credentials. In an
offline environment the ``unreachable`` verdict is the honest answer.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.data.alpaca import FEED_NOTES, feed_label
from app.data.upstox_auth import (
    CREDENTIAL_DOCS,
    GUIDANCE_FORBIDDEN,
    GUIDANCE_REJECTED,
    verify_feed_credential,
)

DATA_HOST = "https://data.alpaca.markets"

ALPACA_GUIDANCE_MISSING = (
    "Set ALPACA_API_KEY and ALPACA_API_SECRET from the Alpaca dashboard "
    "(Market Data section) in the local environment/.env. Use a market-data "
    "key; AXIOM never calls trading endpoints."
)
ALPACA_GUIDANCE_REJECTED = (
    "Alpaca rejected the API key/secret. Verify ALPACA_API_KEY and "
    "ALPACA_API_SECRET (regenerate them on the Alpaca dashboard if needed) "
    "and retry this read-only test."
)
ALPACA_GUIDANCE_FORBIDDEN = (
    "The credential authenticated but is not entitled to the requested feed. "
    "The free Basic plan includes the IEX feed only; SIP requires the paid "
    "subscription. Set ALPACA_DATA_FEED to the feed you are actually entitled "
    "to (iex, sip or delayed_sip) — AXIOM never upgrades the label."
)
ALPACA_GUIDANCE_RATE_LIMITED = (
    "Alpaca rate limit reached (HTTP 429). AXIOM fails closed and never "
    "retries silently. Wait, then retry at the documented plan rate "
    "(Basic: 200 calls/min)."
)
ALPACA_GUIDANCE_UNREACHABLE = (
    "The Alpaca market-data endpoint is unreachable from this network; "
    "connectivity, not the credential, failed first. This is not a verified "
    "connection."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def probe_upstox_connection_sync(
    settings, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20.0,
) -> dict:
    """Synchronous wrapper around :func:`probe_upstox_connection` (CLI/tests)."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(probe_upstox_connection(settings, transport=transport, timeout=timeout))
    raise RuntimeError(
        "probe_upstox_connection_sync cannot run inside an active event loop; "
        "await probe_upstox_connection instead"
    )


async def probe_upstox_connection(
    settings,
    *,
    token_override: str | None = None,
    credential_source: str = "environment",
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 20.0,
) -> dict:
    """Read-only Upstox credential/entitlement probe (awaitable).

    Runs the documented GET authorize probe and returns a public, secret-free
    verdict dict. The token itself is never part of the result;
    ``credential_present`` is a boolean only.
    """
    token = (
        str(token_override)
        if token_override is not None
        else str(getattr(settings, "market_data_access_token", "") or "")
    )
    authorize_url = str(
        getattr(settings, "market_data_authorize_url", "")
        or "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    )
    check = await verify_feed_credential(
        token, authorize_url=authorize_url, transport=transport, timeout=timeout,
    )
    view = check.public_view()
    verdict = view["verdict"]
    if not token.strip():
        conn_state = "DISCONNECTED"
    elif verdict == "feed_ok":
        conn_state = "CONNECTED_READ_ONLY"
    elif verdict in {"rejected", "forbidden"}:
        conn_state = "REJECTED"
    else:
        conn_state = "UNAVAILABLE"
    return {
        "provider": "upstox",
        "market": "india",
        "currency": "INR",
        "credential_present": bool(token.strip()),
        "credential_source": credential_source if token.strip() else "none",
        "connection_state": conn_state,
        "feed": "market-data-feed-v3",
        "feed_label": "UPSTOX MARKET DATA FEED V3 · READ-ONLY · LOCAL PAPER FILLS",
        "instruments_configured": list(
            getattr(settings, "market_data_symbol_list", lambda: [])()
        ),
        "execution_mode": "AXIOM_PAPER",
        "read_only": True,
        "real_order_api_used": False,
        **view,
        "ok": verdict == "feed_ok",
    }


def probe_alpaca_connection(
    settings,
    *,
    symbol: str | None = None,
    credentials_override: dict[str, str] | None = None,
    credential_source: str = "environment",
    transport: httpx.BaseTransport | None = None,
    timeout: float = 20.0,
) -> dict:
    """Read-only Alpaca credential/entitlement probe plus listing metadata.

    Requests one latest-bar read on the configured feed (proves the actual data
    entitlement) and, when a symbol is resolvable, its asset metadata. No
    trading endpoint is touched; the key/secret never appear in the result.
    """
    from app.data.alpaca import AlpacaAssetLookup, validate_us_symbol

    if credentials_override is not None:
        key = str(credentials_override.get("api_key") or "")
        secret = str(credentials_override.get("api_secret") or "")
        feed = str(credentials_override.get("feed") or getattr(settings, "alpaca_data_feed", "") or "iex")
    else:
        key = str(getattr(settings, "alpaca_api_key", "") or "")
        secret = str(getattr(settings, "alpaca_api_secret", "") or "")
        feed = str(getattr(settings, "alpaca_data_feed", "") or "iex")
    configured = list(getattr(settings, "alpaca_symbol_list", lambda: [])())
    requested = (symbol or (configured[0] if configured else "")).strip().upper()
    base = {
        "provider": "alpaca",
        "market": "us",
        "currency": "USD",
        "feed": feed,
        "feed_label": feed_label(feed),
        "feed_is_consolidated": feed == "sip",
        "instruments_configured": configured,
        "execution_mode": "AXIOM_PAPER",
        "read_only": True,
        "real_order_api_used": False,
        "checked_at": _now(),
        "docs": {
            "feed_entitlements": FEED_NOTES,
            "plans": "Alpaca Basic: IEX real-time; SIP requires the paid plan",
        },
    }
    if not key.strip() or not secret.strip():
        return {
            **base,
            "verdict": "invalid_input",
            "authenticated": False,
            "ok": False,
            "guidance": ALPACA_GUIDANCE_MISSING,
            "detail": "No Alpaca credential configured.",
            "credential_present": False,
            "listing": None,
        }
    base["credential_present"] = True
    if not requested:
        return {
            **base,
            "verdict": "invalid_input",
            "authenticated": False,
            "ok": False,
            "guidance": "Configure ALPACA_SYMBOLS or pass a ticker to test.",
            "detail": "No symbol available to probe.",
            "listing": None,
        }
    try:
        requested = validate_us_symbol(requested)
    except ValueError as exc:
        return {
            **base,
            "verdict": "invalid_input",
            "authenticated": False,
            "ok": False,
            "guidance": "Use a plain listed ticker, e.g. AAPL or BRK.B.",
            "detail": str(exc),
            "listing": None,
        }
    base["symbol"] = requested
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    listing = None
    try:
        with httpx.Client(timeout=timeout, transport=transport, headers=headers) as client:
            response = client.get(
                f"{DATA_HOST}/v2/stocks/{requested}/bars/latest",
                params={"feed": feed},
            )
            if response.status_code == 200:
                verdict, authenticated, guidance = (
                    "feed_ok",
                    True,
                    "Credential is entitled to the configured feed (latest-bar read "
                    "succeeded). Safe to start a local PAPER session on this data.",
                )
            elif response.status_code in {401, 403}:
                verdict, authenticated, guidance = (
                    "rejected" if response.status_code == 401 else "forbidden",
                    response.status_code == 403,
                    ALPACA_GUIDANCE_REJECTED
                    if response.status_code == 401 else ALPACA_GUIDANCE_FORBIDDEN,
                )
            elif response.status_code == 429:
                verdict, authenticated, guidance = (
                    "rate_limited", False, ALPACA_GUIDANCE_RATE_LIMITED,
                )
            else:
                verdict, authenticated, guidance = (
                    f"http_{response.status_code}",
                    False,
                    f"Alpaca returned HTTP {response.status_code}; see the official "
                    "documentation before retrying.",
                )
            # Listing metadata (read-only) is best-effort detail, never a
            # substitute for the entitlement verdict above.
            try:
                lookup = AlpacaAssetLookup(key, secret, client=client)
                listing = {"ok": True, "asset": lookup.lookup(requested), "detail": None}
            except ValueError as exc:
                listing = {"ok": False, "asset": None, "detail": str(exc)}
            except httpx.HTTPError:
                listing = {
                    "ok": False,
                    "asset": None,
                    "detail": "Listing metadata unreachable; entitlement verdict unchanged.",
                }
    except httpx.HTTPError:
        return {
            **base,
            "verdict": "unreachable",
            "authenticated": False,
            "ok": False,
            "guidance": ALPACA_GUIDANCE_UNREACHABLE,
            "detail": "Market-data endpoint unreachable from this network.",
            "listing": listing,
        }
    return {
        **base,
        "verdict": verdict,
        "authenticated": authenticated,
        "ok": verdict == "feed_ok" and bool(listing and listing["ok"]),
        "guidance": guidance,
        "detail": f"Latest-bar probe on {requested} ({feed} feed).",
        "listing": listing,
    }
