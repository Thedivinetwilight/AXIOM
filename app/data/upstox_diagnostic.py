"""Safe, read-only Upstox Market Data Feed V3 connectivity diagnostic.

This is deliberately separate from the trading engine. It reads only the
configured market-data token and instrument keys, performs the documented GET
authorize request, and (only when requested) opens a read-only WebSocket
subscription. It never imports a broker, writes configuration, or calls an
order endpoint. Access tokens and the one-use signed WebSocket URL stay in
memory and are never included in output or exception text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from uuid import uuid4

import httpx
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from app.data.upstox_auth import (
    _SSL_CONTEXT,
    _has_tls_error,
    _probe_feed_credential,
)
from app.data.upstox_stream import (
    AUTHORIZE_URL,
    INSTRUMENT_KEY,
    UpstoxProtobufDecoder,
    to_symbol,
)

DEFAULT_LISTEN_SECONDS = 30.0
MAX_LISTEN_SECONDS = 120.0
HTTP_TIMEOUT_SECONDS = 15.0
WS_OPEN_TIMEOUT_SECONDS = 15.0
WS_CLOSE_TIMEOUT_SECONDS = 5.0
MAX_WS_FRAME_BYTES = 1_048_576

# Pass a private, effectively silent logger to websockets. In particular, do
# not let a verbose logger emit the query string from Upstox's one-use URL.
_WS_LOGGER = logging.getLogger("axiom.upstox_diagnostic.websocket")
_WS_LOGGER.addHandler(logging.NullHandler())
_WS_LOGGER.propagate = False
_WS_LOGGER.setLevel(logging.CRITICAL + 1)


def _https_state(check) -> tuple[str, bool | None]:
    """Separate HTTPS/TLS reachability from the credential verdict."""
    if check.http_status is not None:
        # An HTTP response means DNS/TCP and verified TLS all completed.
        return "reachable", True
    if check.verdict == "invalid_input":
        return "not_attempted", None
    if check.verdict == "tls_failure":
        return "tls_failure", False
    if check.verdict == "timeout":
        return "timeout", None
    if check.verdict == "dns_or_connect_failure":
        return "dns_or_tcp_failure", None
    if check.verdict == "protocol_failure":
        return "protocol_failure", None
    return "network_failure", None


def _credential_state(check) -> str:
    return {
        "feed_ok": "authorized",
        "rejected": "rejected_401",
        "forbidden": "forbidden_403",
        "unexpected_response": "malformed_response",
        "invalid_input": "missing_credential",
    }.get(check.verdict, "not_evaluated")


def _subscription_frame(instrument_keys: list[str], mode: str) -> bytes:
    """The documented V3 subscription request; it cannot place orders."""
    request = {
        "guid": uuid4().hex,
        "method": "sub",
        "data": {"mode": mode, "instrumentKeys": instrument_keys},
    }
    return json.dumps(request, separators=(",", ":")).encode("utf-8")


def _has_valid_market_data(events, instrument_keys: set[str]) -> bool:
    """Require a decoded subscribed-instrument tick/snapshot, not just a socket."""
    for event in events:
        if getattr(event, "instrument_key", None) not in instrument_keys:
            continue
        if getattr(event, "kind", None) not in {"live", "snapshot"}:
            continue
        ltp = getattr(event, "ltp", None)
        if isinstance(ltp, (int, float)) and math.isfinite(float(ltp)) and ltp > 0:
            return True
    return False


def _websocket_exception(exc: BaseException) -> tuple[str, int | None, str]:
    """Classify transport errors without rendering exception text or headers."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        guidance = (
            "The WebSocket connection or receive window timed out; this is not an "
            + "invalid-credential verdict."
        )
        return "timeout", None, guidance
    if _has_tls_error(exc):
        guidance = (
            "WebSocket TLS certificate/handshake validation failed; check the PC "
            + "clock, certifi CA bundle, proxy, and antivirus HTTPS inspection."
        )
        return "tls_failure", None, guidance
    if isinstance(exc, InvalidStatus):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            status = getattr(exc, "status_code", None)
        if not isinstance(status, int):
            status = None
        guidance = (
            "The HTTPS authorize probe succeeded, but Upstox refused the WebSocket "
            + "handshake. Check the account/feed permission and retry with a fresh "
            + "authorize URL; this alone does not prove the credential is invalid."
        )
        return "handshake_rejected", status, guidance
    if isinstance(exc, InvalidHandshake):
        guidance = (
            "Upstox did not complete the documented WebSocket handshake; the "
            + "credential verdict remains the separate HTTPS result."
        )
        return "handshake_failure", None, guidance
    if isinstance(exc, (OSError, httpx.ConnectError)):
        guidance = (
            "The WebSocket host could not be reached over DNS/TCP; check network, "
            + "proxy, and firewall settings. The credential was already authorized "
            + "by HTTPS and is not being relabelled invalid."
        )
        return "dns_or_tcp_failure", None, guidance
    guidance = (
        f"The WebSocket check failed ({type(exc).__name__}); authorization was "
        + "evaluated separately. No response body, headers, or URL were emitted."
    )
    return "websocket_failure", None, guidance


def _default_websocket_connector(uri: str, **kwargs):
    # Import lazily so an HTTPS-only diagnostic doesn't need to start the WS stack.
    from websockets.asyncio.client import connect

    return connect(uri, **kwargs)


async def _probe_websocket(
    websocket_uri: str,
    instrument_keys: list[str],
    mode: str,
    duration: float,
    *,
    websocket_connector=None,
    decoder_factory=None,
) -> tuple[dict, dict]:
    websocket = {
        "requested": True,
        "handshake": "not_attempted",
        "handshake_completed": False,
        "http_status": None,
        "guidance": "",
    }
    market_data = {
        "received": False,
        "valid_messages": 0,
        "frames_received": 0,
        "invalid_frames": 0,
        "listen_window_seconds": duration,
        "elapsed_seconds": 0.0,
        "reason": "",
    }

    keys = list(dict.fromkeys(str(key).strip() for key in instrument_keys if str(key).strip()))
    if not keys:
        websocket["guidance"] = (
            "No instrument keys are configured. Set MARKET_DATA_SYMBOLS or "
            "UPSTOX_INSTRUMENT_KEYS before requesting a feed subscription."
        )
        market_data["reason"] = "no_instrument_keys_configured"
        return websocket, market_data
    if any(not INSTRUMENT_KEY.fullmatch(key) for key in keys):
        websocket["guidance"] = (
            "One or more configured instrument keys are malformed; correct "
            "MARKET_DATA_SYMBOLS / UPSTOX_INSTRUMENT_KEYS before retrying."
        )
        market_data["reason"] = "invalid_instrument_keys"
        return websocket, market_data
    if mode not in {"ltpc", "full"}:
        websocket["guidance"] = "MARKET_DATA_MODE must be ltpc or full."
        market_data["reason"] = "invalid_market_data_mode"
        return websocket, market_data

    decoder_factory = decoder_factory or UpstoxProtobufDecoder
    decoder = decoder_factory({key: to_symbol(key) for key in keys}, mode)
    connector = websocket_connector or _default_websocket_connector
    started = time.monotonic()
    try:
        # async-with sends a normal WebSocket close frame on every exit path.
        # The single-use URI and its query string are only passed in memory.
        async with connector(
            websocket_uri,
            ssl=_SSL_CONTEXT,
            open_timeout=min(WS_OPEN_TIMEOUT_SECONDS, duration),
            close_timeout=WS_CLOSE_TIMEOUT_SECONDS,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_WS_FRAME_BYTES,
            logger=_WS_LOGGER,
        ) as socket:
            websocket["handshake"] = "connected"
            websocket["handshake_completed"] = True
            await socket.send(_subscription_frame(keys, mode))

            deadline = asyncio.get_running_loop().time() + duration
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    market_data["reason"] = "no_valid_market_data_in_window"
                    break
                try:
                    frame = await asyncio.wait_for(socket.recv(), timeout=remaining)
                except TimeoutError:
                    market_data["reason"] = "no_valid_market_data_in_window"
                    break
                except ConnectionClosed:
                    websocket["handshake"] = "closed"
                    websocket["guidance"] = (
                        "Upstox closed the WebSocket after the handshake, before a "
                        "valid subscribed-instrument message was received."
                    )
                    market_data["reason"] = "connection_closed_before_valid_message"
                    break

                market_data["frames_received"] += 1
                if not isinstance(frame, bytes):
                    # Error/control text is deliberately not shown; Upstox V3
                    # market-feed payloads are binary protobuf frames.
                    market_data["invalid_frames"] += 1
                    continue
                try:
                    events = decoder.decode(frame, time.time())
                except Exception:  # noqa: BLE001 - upstream frames are untrusted input
                    # Do not echo arbitrary frame data or decoder exception text.
                    market_data["invalid_frames"] += 1
                    continue
                if _has_valid_market_data(events, set(keys)):
                    market_data["received"] = True
                    market_data["valid_messages"] = 1
                    market_data["reason"] = "valid_subscribed_instrument_message_received"
                    break
            market_data["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if not market_data["reason"]:
                market_data["reason"] = "no_valid_market_data_in_window"
            if websocket["handshake"] == "connected":
                websocket["guidance"] = (
                    "WebSocket handshake completed. The connection was closed cleanly "
                    "after the bounded read; no order endpoint was used."
                )
    except InvalidStatus as exc:
        state, status, guidance = _websocket_exception(exc)
        websocket.update(handshake=state, http_status=status, guidance=guidance)
        market_data["reason"] = "websocket_handshake_rejected"
        market_data["elapsed_seconds"] = round(time.monotonic() - started, 3)
    except (TimeoutError, httpx.TimeoutException) as exc:
        state, status, guidance = _websocket_exception(exc)
        websocket.update(handshake=state, http_status=status, guidance=guidance)
        market_data["reason"] = "websocket_timeout"
        market_data["elapsed_seconds"] = round(time.monotonic() - started, 3)
    except ConnectionClosed:
        websocket.update(
            handshake="closed",
            guidance="Upstox closed the WebSocket before a market-data message arrived.",
        )
        market_data["reason"] = "connection_closed_before_valid_message"
        market_data["elapsed_seconds"] = round(time.monotonic() - started, 3)
    except Exception as exc:  # noqa: BLE001 - sanitize all untrusted WS failures
        state, status, guidance = _websocket_exception(exc)
        websocket.update(handshake=state, http_status=status, guidance=guidance)
        market_data["reason"] = state
        market_data["elapsed_seconds"] = round(time.monotonic() - started, 3)

    return websocket, market_data


async def run_upstox_diagnostic(
    token: str,
    *,
    instrument_keys: list[str] | None = None,
    mode: str = "ltpc",
    test_websocket: bool = False,
    duration: float = DEFAULT_LISTEN_SECONDS,
    authorize_url: str = AUTHORIZE_URL,
    http_transport: httpx.AsyncBaseTransport | None = None,
    websocket_connector=None,
    decoder_factory=None,
    http_timeout: float = HTTP_TIMEOUT_SECONDS,
) -> dict:
    """Run a bounded read-only credential check and optional real feed probe.

    ``authorize_url``, ``http_transport``, ``websocket_connector`` and
    ``decoder_factory`` are test seams; the CLI never accepts a URL or token
    argument and always uses Upstox's documented authorization endpoint.
    The actual configured token is read by the CLI from ``Settings.from_env``.
    """
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        raise ValueError("duration must be a finite number between 0 and 120 seconds")
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_LISTEN_SECONDS:
        raise ValueError("duration must be a finite number between 0 and 120 seconds")

    outcome = await _probe_feed_credential(
        token,
        authorize_url=authorize_url,
        transport=http_transport,
        timeout=http_timeout,
    )
    check = outcome.check
    https_status, tls_verified = _https_state(check)
    report = {
        "command": "upstox-test",
        "ok": False,
        "https_reachability": {
            "status": https_status,
            "tls_verified": tls_verified,
            "http_status": check.http_status,
        },
        "credential_authorization": {
            "status": _credential_state(check),
            "verdict": check.verdict,
            "authenticated": check.authenticated,
            "http_status": check.http_status,
            "guidance": check.guidance,
            "docs": check.public_view()["docs"],
        },
        # Preserve the original CLI's simple fields while adding explicit,
        # independently machine-readable handshake and market-data results.
        "credential_check": check.public_view(),
        "authenticated": check.authenticated,
        "websocket": "not_tried" if test_websocket else "not_requested",
        "websocket_check": {
            "requested": bool(test_websocket),
            "handshake": "not_requested" if not test_websocket else "not_attempted",
            "handshake_completed": False,
            "http_status": None,
            "guidance": "",
        },
        "market_data": {
            "received": False,
            "valid_messages": 0,
            "frames_received": 0,
            "invalid_frames": 0,
            "listen_window_seconds": duration if test_websocket else 0.0,
            "elapsed_seconds": 0.0,
            "reason": "not_requested" if not test_websocket else "authorization_not_successful",
        },
        "scope": {
            "read_only": True,
            "order_endpoints_called": False,
            "configuration_written": False,
            "live_trading_enabled_by_diagnostic": False,
        },
    }

    if check.verdict == "invalid_input":
        # Backward-compatible, safe explanation for scripts using the old CLI
        # error field. It contains only the setting names, never a token value.
        report["error"] = check.guidance

    if check.verdict != "feed_ok":
        report["websocket_check"]["guidance"] = (
            "WebSocket was not attempted because the credential did not pass the "
            "HTTPS authorization probe."
        )
        return report

    if not test_websocket:
        report["ok"] = True
        report["websocket_check"]["guidance"] = (
            "Not requested. Re-run with --websocket to test the live feed."
        )
        return report

    websocket, market_data = await _probe_websocket(
        outcome.websocket_uri or "",
        list(instrument_keys or []),
        mode,
        duration,
        websocket_connector=websocket_connector,
        decoder_factory=decoder_factory,
    )
    report["websocket_check"] = websocket
    report["websocket"] = "connected" if websocket["handshake_completed"] else websocket["handshake"]
    report["market_data"] = market_data
    report["ok"] = bool(websocket["handshake_completed"] and market_data["received"])
    return report
