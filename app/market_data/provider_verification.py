"""Unified read-only provider connection & quote verification across NSE and NASDAQ.

Standardizes the 6-state operator connection model required across all providers:
- ``DISCONNECTED``: no credential configured in protected store or environment
- ``AUTHENTICATING``: verification request currently in flight (UI transition state)
- ``CONNECTED_READ_ONLY``: authenticated and verified with a fresh read-only quote/feed
- ``STALE``: authenticated read-only, but latest quote is outside active session,
  older than freshness window, or on a 15m delayed feed (explicitly labeled)
- ``REJECTED``: HTTP 401/403 invalid/expired token or missing plan/feed entitlement
- ``UNAVAILABLE``: network unreachable, HTTP 429 rate-limited, or external eligibility locked

No order endpoint is ever called from this module, and no secret or token
character is ever echoed, logged, or stored here.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Any

import httpx

from app.autonomy.upstox import (
    InvalidQuote,
    NoTradedQuote,
    ReconciliationFailure,
    UpstoxAuthError,
    UpstoxReadOnly,
)
from app.autonomy.upstox_token import resolve_live_token, resolve_token
from app.data.alpaca import AlpacaAssetLookup, AlpacaQuoteReader, feed_label, validate_us_symbol
from app.markets.alpaca_credentials import resolve_alpaca_data_credentials
from app.markets.nse_reconciliation import nse_session_status
from app.markets.us_calendar import USEquitiesCalendar

PROVIDER_CONNECTION_STATES: tuple[str, ...] = (
    "DISCONNECTED",
    "AUTHENTICATING",
    "CONNECTED_READ_ONLY",
    "STALE",
    "REJECTED",
    "UNAVAILABLE",
)

DEFAULT_NSE_TEST_KEY = "NSE_EQ|INE002A01018"
DEFAULT_NASDAQ_TEST_SYMBOL = "AAPL"


def inspect_jwt_expiry(token: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Extract unverified JWT `exp` metadata purely for operator expiry warnings.

    Never trusts unverified claims for authorization and never returns any
    substring of the token itself.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    base = {
        "daily_reset_schedule": "Upstox access tokens expire daily (~03:30 IST) and require fresh authorization.",
        "is_jwt": False,
        "jwt_exp_present": False,
        "expires_at": None,
        "seconds_until_expiry": None,
        "seconds_remaining": None,
        "expired_by_claim": False,
        "expired": False,
        "scopes": [],
    }
    raw = (token or "").strip()
    parts = raw.split(".")
    if len(parts) != 3:
        return base
    payload_b64 = parts[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except Exception:
        return base
    if not isinstance(claims, dict) or not isinstance(claims.get("exp"), (int, float)):
        return base
    exp_dt = datetime.fromtimestamp(float(claims["exp"]), tz=timezone.utc)
    remaining = round((exp_dt - current).total_seconds(), 1)
    raw_scopes = claims.get("scope") or claims.get("scopes") or []
    scopes = [str(s) for s in raw_scopes] if isinstance(raw_scopes, list) else str(raw_scopes).split()
    return {
        **base,
        "is_jwt": True,
        "jwt_exp_present": True,
        "expires_at": exp_dt.isoformat(),
        "seconds_until_expiry": remaining,
        "seconds_remaining": remaining,
        "expired_by_claim": remaining <= 0,
        "expired": remaining <= 0,
        "scopes": scopes,
    }


def verify_upstox_market_data(
    token: str,
    *,
    credential_source: str = "none",
    instrument_key: str = DEFAULT_NSE_TEST_KEY,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    freshness_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run a harmless read-only Upstox profile + market-quote check."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    session = nse_session_status(current)
    clean_token = (token or "").strip()
    base = {
        "provider": "upstox_market_data",
        "role": "MARKET_DATA_READ_ONLY",
        "market": "NSE",
        "currency": "INR",
        "read_only": True,
        "order_endpoints_used": False,
        "real_order_api_used": False,
        "credential_present": bool(clean_token),
        "credential_source": credential_source if clean_token else "none",
        "checked_at": current.isoformat(),
        "session": session,
        "instrument_key": instrument_key,
    }
    if not clean_token:
        return {
            **base,
            "ok": False,
            "authenticated": False,
            "connection_state": "DISCONNECTED",
            "verdict": "missing_credentials",
            "token_expiry": inspect_jwt_expiry("", now=current),
            "quote": None,
            "guidance": (
                "Paste a valid daily Upstox v2/v3 access_token in Connections -> Upstox Market Data "
                "(stored encrypted via Windows DPAPI) or set UPSTOX_ACCESS_TOKEN."
            ),
        }

    expiry_info = inspect_jwt_expiry(clean_token, now=current)
    if expiry_info["expired_by_claim"]:
        return {
            **base,
            "ok": False,
            "authenticated": False,
            "connection_state": "REJECTED",
            "verdict": "expired_jwt",
            "token_expiry": expiry_info,
            "quote": None,
            "guidance": (
                "Upstox access token JWT is expired (exp claim is in the past). "
                "Generate a fresh daily Upstox token (~03:30 IST reset) and save it."
            ),
        }
    reader = UpstoxReadOnly(lambda: clean_token, transport=transport, clock=lambda: current)
    try:
        prof = reader.profile(include_static_ips=False)
    except UpstoxAuthError:
        return {
            **base,
            "ok": False,
            "authenticated": False,
            "connection_state": "REJECTED",
            "verdict": "rejected",
            "token_expiry": expiry_info,
            "quote": None,
            "guidance": (
                "Upstox rejected or expired this access token (HTTP 401/403). Upstox tokens "
                "expire daily (~03:30 IST); generate a fresh daily token and save it."
            ),
        }
    except ReconciliationFailure as exc:
        msg = str(exc)
        is_rate = "rate limit" in msg.lower() or "429" in msg
        return {
            **base,
            "ok": False,
            "authenticated": False,
            "connection_state": "UNAVAILABLE",
            "verdict": "rate_limited" if is_rate else "unreachable",
            "token_expiry": expiry_info,
            "quote": None,
            "detail": msg[:240],
            "guidance": (
                "Upstox read rate limit reached (HTTP 429). Wait briefly and retry."
                if is_rate
                else "Upstox API is unreachable from this host/network; connection is NOT verified."
            ),
        }

    nse_entitled = "NSE" in prof.exchanges or "NSE_EQ" in prof.exchanges
    profile_view = {
        "active": prof.active,
        "exchanges": sorted(prof.exchanges),
        "products": sorted(prof.products),
        "nse_entitled": nse_entitled,
        "user_fingerprint": prof.user_fingerprint,
    }
    if not prof.active or not nse_entitled:
        return {
            **base,
            "ok": False,
            "authenticated": True,
            "connection_state": "REJECTED",
            "verdict": "missing_nse_entitlement",
            "profile": profile_view,
            "token_expiry": expiry_info,
            "quote": None,
            "guidance": (
                "Upstox account authenticated, but NSE equity segment is not active/enabled "
                f"(exchanges={sorted(prof.exchanges)}). Enable NSE cash segment in Upstox."
            ),
        }

    try:
        snap = reader.quote_snapshot(instrument_key)
    except NoTradedQuote as exc:
        state = "STALE" if not session["is_open"] else "UNAVAILABLE"
        return {
            **base,
            "ok": not session["is_open"],
            "authenticated": True,
            "connection_state": state,
            "verdict": "no_traded_quote_off_hours" if not session["is_open"] else "no_traded_quote",
            "profile": profile_view,
            "token_expiry": expiry_info,
            "quote": {
                "instrument_key": instrument_key,
                "last": None,
                "timestamp": exc.timestamp.isoformat() if exc.timestamp else None,
                "fresh": False,
                "freshness_label": "NO_TRADE_OFF_HOURS" if not session["is_open"] else "NO_TRADED_PRICE",
                "source": "upstox-market-quote-v2",
            },
            "detail": str(exc)[:240],
            "guidance": (
                "Upstox authentication and NSE entitlement succeeded. Market is currently closed "
                f"({session['state']}); no live trade print is substituted."
                if not session["is_open"]
                else str(exc)[:240]
            ),
        }
    except (InvalidQuote, ReconciliationFailure, UpstoxAuthError) as exc:
        msg = str(exc)
        is_rate = "rate limit" in msg.lower() or "429" in msg
        return {
            **base,
            "ok": False,
            "authenticated": True,
            "connection_state": "UNAVAILABLE",
            "verdict": "rate_limited" if is_rate else "quote_failed",
            "profile": profile_view,
            "token_expiry": expiry_info,
            "quote": None,
            "detail": msg[:240],
            "guidance": "Authenticated profile, but market-quote read failed; failing closed.",
        }

    age_seconds = round(max(0.0, (current - snap.timestamp).total_seconds()), 2)
    is_fresh = age_seconds <= freshness_seconds and session["is_open"]
    state = "CONNECTED_READ_ONLY" if is_fresh else "STALE"
    freshness_label = (
        "FRESH"
        if is_fresh
        else ("STALE_MARKET_CLOSED" if not session["is_open"] else "STALE_QUOTE")
    )
    return {
        **base,
        "ok": True,
        "authenticated": True,
        "connection_state": state,
        "verdict": "connected_fresh" if is_fresh else "connected_stale",
        "profile": profile_view,
        "token_expiry": expiry_info,
        "quote": {
            "instrument_key": snap.instrument_key,
            "bid": snap.bid,
            "ask": snap.ask,
            "last": snap.last,
            "volume": snap.volume,
            "timestamp": snap.timestamp.isoformat(),
            "age_seconds": age_seconds,
            "fresh": is_fresh,
            "freshness": freshness_label,
            "freshness_label": freshness_label,
            "currency": "INR",
            "source": "upstox-market-quote-v2",
        },
        "guidance": (
            f"Upstox read-only NSE feed verified with fresh quote ({age_seconds:.1f}s old)."
            if is_fresh
            else (
                f"Upstox authenticated read-only; last NSE trade timestamp is {age_seconds:.0f}s old "
                f"({session['state']}). Shown as STALE/closing reference, never disguised as live."
            )
        ),
    }


def verify_alpaca_market_data(
    api_key: str,
    api_secret: str,
    *,
    feed: str = "iex",
    credential_source: str = "none",
    symbol: str = DEFAULT_NASDAQ_TEST_SYMBOL,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    freshness_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run a harmless read-only Alpaca snapshot + listing check on NASDAQ."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    calendar = USEquitiesCalendar()
    session = calendar.market_state(current)
    key = (api_key or "").strip()
    secret = (api_secret or "").strip()
    feed_norm = (feed or "iex").strip().lower()
    if feed_norm not in {"iex", "sip", "delayed_sip"}:
        feed_norm = "iex"

    try:
        clean_symbol = validate_us_symbol((symbol or DEFAULT_NASDAQ_TEST_SYMBOL).strip().upper())
    except ValueError as exc:
        return {
            "provider": "alpaca_market_data",
            "role": "MARKET_DATA_READ_ONLY",
            "market": "NASDAQ",
            "currency": "USD",
            "ok": False,
            "authenticated": False,
            "connection_state": "UNAVAILABLE",
            "verdict": "invalid_symbol",
            "detail": str(exc),
            "guidance": "Provide a valid listed US ticker such as AAPL, MSFT, NVDA, or QQQ.",
        }

    base = {
        "provider": "alpaca_market_data",
        "role": "MARKET_DATA_READ_ONLY",
        "market": "NASDAQ",
        "currency": "USD",
        "symbol": clean_symbol,
        "feed": feed_norm,
        "feed_label": feed_label(feed_norm),
        "feed_is_consolidated": feed_norm == "sip",
        "read_only": True,
        "order_endpoints_used": False,
        "real_order_api_used": False,
        "credential_present": bool(key and secret),
        "credential_source": credential_source if (key and secret) else "none",
        "checked_at": current.isoformat(),
        "session": session,
    }
    if not key or not secret:
        return {
            **base,
            "ok": False,
            "authenticated": False,
            "connection_state": "DISCONNECTED",
            "verdict": "missing_credentials",
            "quote": None,
            "listing": None,
            "guidance": (
                "Save Alpaca Market Data API Key & Secret in Connections -> Alpaca Market Data "
                "(encrypted via Windows DPAPI) or set ALPACA_API_KEY and ALPACA_API_SECRET."
            ),
        }

    http_client = httpx.Client(transport=transport, timeout=15.0)
    try:
        quote_reader = AlpacaQuoteReader(key, secret, feed=feed_norm, client=http_client)
        quote = quote_reader.latest_quote(clean_symbol)
        listing = None
        try:
            lookup = AlpacaAssetLookup(key, secret, client=http_client)
            listing = lookup.lookup(clean_symbol)
        except Exception as exc:
            listing = {"symbol": clean_symbol, "error": str(exc)[:160]}
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 401:
            return {
                **base,
                "ok": False,
                "authenticated": False,
                "connection_state": "REJECTED",
                "verdict": "rejected",
                "quote": None,
                "listing": None,
                "guidance": "Alpaca rejected the Market Data API key/secret (HTTP 401). Regenerate keys and retry.",
            }
        if code == 403:
            return {
                **base,
                "ok": False,
                "authenticated": True,
                "connection_state": "REJECTED",
                "verdict": "forbidden_feed_entitlement",
                "quote": None,
                "listing": None,
                "guidance": (
                    f"Account is not entitled to feed={feed_norm!r} (HTTP 403). Free Alpaca Basic plans "
                    "support 'iex' real-time or 'delayed_sip'; full real-time 'sip' requires Algo Trader Plus."
                ),
            }
        return {
            **base,
            "ok": False,
            "authenticated": False,
            "connection_state": "UNAVAILABLE",
            "verdict": f"http_{code}",
            "quote": None,
            "listing": None,
            "guidance": f"Alpaca Market Data returned HTTP {code}; failing closed.",
        }
    except RuntimeError as exc:
        msg = str(exc)
        is_rate = "429" in msg or "rate limit" in msg.lower()
        return {
            **base,
            "ok": False,
            "authenticated": False,
            "connection_state": "UNAVAILABLE",
            "verdict": "rate_limited" if is_rate else "error",
            "detail": msg[:240],
            "quote": None,
            "listing": None,
            "guidance": (
                "Alpaca rate limit reached (HTTP 429). Wait and retry within plan limits."
                if is_rate
                else msg[:240]
            ),
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            **base,
            "ok": False,
            "authenticated": False,
            "connection_state": "UNAVAILABLE",
            "verdict": "unreachable" if isinstance(exc, httpx.HTTPError) else "invalid_quote",
            "detail": str(exc)[:240],
            "quote": None,
            "listing": None,
            "guidance": "Alpaca Market Data endpoint unreachable or returned incomplete quote; not verified.",
        }
    finally:
        http_client.close()

    from app.data.models import parse_timestamp

    quote_ts = parse_timestamp(quote["timestamp"])
    age_seconds = round(max(0.0, (current - quote_ts).total_seconds()), 2)
    is_delayed = feed_norm == "delayed_sip"
    is_fresh = (not is_delayed) and age_seconds <= freshness_seconds and bool(session["market_open"])
    state = "CONNECTED_READ_ONLY" if is_fresh else "STALE"
    freshness_label = (
        "FRESH"
        if is_fresh
        else (
            "DELAYED_15M"
            if is_delayed
            else ("STALE_MARKET_CLOSED" if not session["market_open"] else "STALE_QUOTE")
        )
    )
    quote_out = {
        **quote,
        "age_seconds": age_seconds,
        "fresh": is_fresh,
        "freshness": freshness_label,
        "freshness_label": freshness_label,
    }
    return {
        **base,
        "ok": True,
        "authenticated": True,
        "connection_state": state,
        "verdict": "connected_fresh" if is_fresh else "connected_stale",
        "quote": quote_out,
        "listing": listing,
        "guidance": (
            f"Alpaca {feed_norm.upper()} read-only feed verified for {clean_symbol} (${quote['last']:.2f}, {age_seconds:.1f}s old)."
            if is_fresh
            else (
                f"Alpaca {feed_norm.upper()} authenticated read-only for {clean_symbol} (${quote['last']:.2f}); "
                f"marked {freshness_label} (age {age_seconds:.0f}s, session {session['status']})."
            )
        ),
    }


def build_provider_matrix(app_state: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Summarize all configured market-data and execution roles without calling order routes."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    settings = getattr(app_state, "settings", None)

    upstox_tok, upstox_src = resolve_token(getattr(app_state, "upstox_token", None))
    if not upstox_tok and settings is not None:
        env_tok = str(getattr(settings, "market_data_access_token", "") or "").strip()
        if env_tok:
            upstox_tok, upstox_src = env_tok, "environment"

    upstox_live_tok, upstox_live_src = resolve_live_token(getattr(app_state, "upstox_live_token", None))
    autonomy = getattr(app_state, "autonomy", None)
    autonomy_live_flag = bool(getattr(getattr(autonomy, "settings", None), "live_enabled", False))

    alpaca_data, alpaca_data_src = resolve_alpaca_data_credentials(
        getattr(app_state, "alpaca_data_store", None),
        settings,
    )

    broker_rt = getattr(app_state, "alpaca_broker_runtime", None)
    broker_creds = broker_rt.credential_status() if broker_rt is not None else {
        "enabled": False,
        "live_master_flag": False,
        "paper": {"present": False, "source": "none"},
        "live": {"present": False, "source": "none"},
    }

    nse_sess = nse_session_status(current)
    us_sess = USEquitiesCalendar().market_state(current)

    providers = [
        {
            "id": "upstox_market_data",
            "name": "Upstox Market Data (NSE / BSE)",
            "market": "NSE",
            "currency": "INR",
            "role": "MARKET_DATA_READ_ONLY",
            "used_for": ["Watchlists", "Supercharts", "LIVE-MARKET PAPER (NSE INR)"],
            "order_capable": False,
            "credential_present": bool(upstox_tok),
            "credential_source": upstox_src,
            "connection_state": "CONNECTED_READ_ONLY" if upstox_tok and nse_sess["is_open"] else ("STALE" if upstox_tok else "DISCONNECTED"),
            "token_expiry": inspect_jwt_expiry(upstox_tok, now=current) if upstox_tok else None,
            "session": nse_sess,
            "verification_note": "Click 'Verify Quote' to run a live read-only profile + NSE quote probe.",
        },
        {
            "id": "upstox_live_execution",
            "name": "Upstox LIVE Execution (NSE Real Money)",
            "market": "NSE",
            "currency": "INR",
            "role": "LIVE_EXECUTION_GATED",
            "used_for": ["LIVE-MARKET LIVE (NSE INR — Protected GTT v3)"],
            "order_capable": True,
            "default_state": "OFF",
            "live_master_flag": autonomy_live_flag,
            "credential_present": bool(upstox_live_tok),
            "credential_source": upstox_live_src,
            "connection_state": (
                "UNAVAILABLE"
                if not autonomy_live_flag
                else ("DISCONNECTED" if not upstox_live_tok else "CONNECTED_READ_ONLY")
            ),
            "availability": (
                "AVAILABLE_BEHIND_GATE"
                if (autonomy_live_flag and upstox_live_tok)
                else "NOT_AVAILABLE"
            ),
            "external_dependencies": [
                "Set AUTONOMY_LIVE_ENABLED=true",
                "Configure UPSTOX_ALGO_NAME and SEBI-mandated registered static IP on Upstox",
                "Save separate UPSTOX_LIVE_ACCESS_TOKEN in Windows DPAPI",
                "Pass readiness checks, then click Arm followed by Activate",
            ],
        },
        {
            "id": "alpaca_market_data",
            "name": "Alpaca Market Data (NASDAQ / US)",
            "market": "NASDAQ",
            "currency": "USD",
            "role": "MARKET_DATA_READ_ONLY",
            "used_for": ["Watchlists", "Supercharts", "LIVE-MARKET PAPER (NASDAQ USD)"],
            "order_capable": False,
            "feed": alpaca_data["feed"],
            "feed_label": feed_label(alpaca_data["feed"]),
            "credential_present": bool(alpaca_data["api_key"] and alpaca_data["api_secret"]),
            "credential_source": alpaca_data_src,
            "connection_state": (
                ("CONNECTED_READ_ONLY" if us_sess["market_open"] and alpaca_data["feed"] != "delayed_sip" else "STALE")
                if (alpaca_data["api_key"] and alpaca_data["api_secret"])
                else "DISCONNECTED"
            ),
            "session": us_sess,
            "verification_note": "Click 'Verify Quote' to run a live read-only NASDAQ snapshot + listing probe.",
        },
        {
            "id": "alpaca_broker_paper",
            "name": "Alpaca Broker Paper (Broker-Simulated USD)",
            "market": "NASDAQ",
            "currency": "USD",
            "role": "BROKER_PAPER_OPTIONAL",
            "used_for": ["Optional Broker-Side Paper Execution (separate from AXIOM Local Paper)"],
            "order_capable": True,
            "real_money": False,
            "credential_present": bool(broker_creds["paper"]["present"]),
            "credential_source": broker_creds["paper"]["source"],
            "connection_state": "CONNECTED_READ_ONLY" if broker_creds["paper"]["present"] else "DISCONNECTED",
        },
        {
            "id": "alpaca_broker_live",
            "name": "Alpaca Broker LIVE (NASDAQ Real Money)",
            "market": "NASDAQ",
            "currency": "USD",
            "role": "LIVE_EXECUTION_GATED",
            "used_for": ["LIVE-MARKET LIVE (NASDAQ USD — Real Money)"],
            "order_capable": True,
            "real_money": True,
            "default_state": "OFF",
            "live_master_flag": bool(broker_creds["live_master_flag"]),
            "credential_present": bool(broker_creds["live"]["present"]),
            "credential_source": broker_creds["live"]["source"],
            "connection_state": (
                "UNAVAILABLE"
                if not broker_creds["live_master_flag"]
                else ("DISCONNECTED" if not broker_creds["live"]["present"] else "CONNECTED_READ_ONLY")
            ),
            "availability": (
                "AVAILABLE_BEHIND_GATE"
                if (broker_creds["live_master_flag"] and broker_creds["live"]["present"])
                else "NOT_AVAILABLE"
            ),
            "external_dependencies": [
                "Eligible Alpaca Live Margin/Cash Brokerage Account (USD, unblocked)",
                "Set ALPACA_BROKER_ENABLED=true in host environment",
                "Save distinct ALPACA_BROKER_LIVE API key & secret in Windows DPAPI",
                "Configure positive bounded risk limits, verify fresh IEX/SIP quote, Arm, then Activate with phrase",
            ],
        },
    ]
    return {
        "checked_at": current.isoformat(),
        "supported_states": list(PROVIDER_CONNECTION_STATES),
        "providers": providers,
        "tradingview_notice": (
            "Charting UI is AXIOM's local dark canvas inspired by TradingView ergonomics. "
            "No TradingView data feed or TradingView brokerage integration is used or implied."
        ),
    }
