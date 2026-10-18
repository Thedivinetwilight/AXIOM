"""Upstox OAuth 2.0 authorization-code helper — per CURRENT official docs.

Verified against https://upstox.com/developer/api-documentation/authentication/
(fetched 2026-09-21):

1. Send the user to the login dialog:
   https://api.upstox.com/v2/login/authorization/dialog
       ?response_type=code&client_id=<API key>&redirect_uri=<registered>&state=<optional>
2. Upstox redirects to redirect_uri with ?code=<single-use code>[&state=...]
3. Server-to-server exchange:
   POST https://api.upstox.com/v2/login/authorization/token
   Content-Type: application/x-www-form-urlencoded
   code, client_id, client_secret, redirect_uri, grant_type=authorization_code

Secrets (client_secret, access_token) are never logged, printed, or embedded in
exceptions. Tokens obtained here belong in `.env` (gitignored), never in code.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import getpass
import os
import ssl
import stat

import certifi
import httpx

# When this module is frozen by PyInstaller, certifi's cacert.pem lives at an
# on-disk path adjacent to the certifi package inside the bundle. PyInstaller
# may not ship it without an explicit collect, so resolve it through certifi's
# API (which the spec's `collect_data_files('certifi')` will include) and pass
# it explicitly to httpx; without this, outbound HTTPS fails on Windows with an
# unhelpful "Failed to fetch" because there is no trust store to verify the
# Upstox certificate chain against.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

LOGIN_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"

# Official documentation verified 2026-09-22 (V0.6.3):
# - Standard OAuth login access tokens: daily, full API, the documented path
#   for Market Data Feed V3 (authentication/ page).
# - Analytics Tokens: 1-year, strictly read-only GET APIs across listed
#   categories — which DO include a "Websocket" category — but feed access
#   must be VERIFIED BEHAVIORALLY and never assumed (analytics-token/ page).
# Tokens are opaque strings: no reliable offline distinction exists, so the
# project verifies feed access with the documented GET authorize call and
# reports an explicit verdict BEFORE any WebSocket connection is attempted.
CREDENTIAL_DOCS = {
    "oauth_login": "https://upstox.com/developer/api-documentation/authentication/",
    "analytics_token": "https://upstox.com/developer/api-documentation/analytics-token/",
    "feed_authorize": "https://upstox.com/developer/api-documentation/get-market-data-feed-authorize-v3/",
}

GUIDANCE_REJECTED = (
    "Upstox returned HTTP 401 from the Market Data Feed V3 authorize endpoint: "
    "it did not accept this bearer access token. HTTP 401 alone cannot distinguish "
    "an expired, revoked, malformed, or unsupported token; the response body is "
    "intentionally omitted. Obtain a fresh standard OAuth login token with "
    "python -m app upstox-login, confirm it is stored as "
    "MARKET_DATA_ACCESS_TOKEN or UPSTOX_ACCESS_TOKEN, and retry. Do not assume an "
    "Analytics Token works for the V3 feed; access is verified behaviorally."
)
GUIDANCE_FORBIDDEN = (
    "Upstox returned HTTP 403 from the Market Data Feed V3 authorize endpoint: "
    "the request reached Upstox, but this account/token was refused feed access. "
    "This is an authorization/permission result, not a network or TLS failure. "
    "Check the account and app's Market Data Feed permissions, use the standard "
    "OAuth login access token documented for the feed, and contact Upstox if the 403 "
    "continues. The response body is intentionally omitted."
)


class UpstoxOAuthError(RuntimeError):
    """OAuth flow failure. Messages never contain codes, secrets, or tokens."""


@dataclass(frozen=True)
class CredentialCheck:
    """Behavioral credential verdict. Never stores the token itself."""

    verdict: str  # feed_ok | rejected | forbidden | unexpected_response | unreachable | invalid_input
    http_status: int | None
    authenticated: bool
    guidance: str
    checked_at: str

    def public_view(self) -> dict:
        return {
            "verdict": self.verdict,
            "http_status": self.http_status,
            "authenticated": self.authenticated,
            "guidance": self.guidance,
            "checked_at": self.checked_at,
            "docs": CREDENTIAL_DOCS,
        }


@dataclass(frozen=True)
class _FeedAuthorizationProbe:
    """Internal authorization result; the one-use socket URL is never public."""

    check: CredentialCheck
    websocket_uri: str | None = field(default=None, repr=False, compare=False)


def _has_tls_error(exc: BaseException) -> bool:
    """Find TLS/certificate errors wrapped by httpx/httpcore connect errors."""
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        for wrapped in (current.__cause__, current.__context__):
            if isinstance(wrapped, BaseException):
                pending.append(wrapped)
    return False


def _is_upstox_websocket_uri(uri: object) -> bool:
    """Accept only secure Upstox hosts before handing over its one-use URL."""
    if not isinstance(uri, str):
        return False
    try:
        parsed = urlsplit(uri)
        host = (parsed.hostname or "").rstrip(".").lower()
        return (
            parsed.scheme.lower() == "wss"
            and (host == "upstox.com" or host.endswith(".upstox.com"))
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
    except ValueError:
        return False


async def _probe_feed_credential(
    token: str,
    *,
    authorize_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 20.0,
) -> _FeedAuthorizationProbe:
    """Internal read-only probe. The signed one-use URI stays in memory only."""
    now = datetime.now(timezone.utc).isoformat()

    def result(verdict, status, authenticated, guidance, websocket_uri=None):
        return _FeedAuthorizationProbe(
            CredentialCheck(verdict, status, authenticated, guidance, now),
            websocket_uri,
        )

    if not isinstance(token, str) or not token.strip():
        return result(
            "invalid_input", None, False,
            "No Upstox market-data access token is configured. Set "
            "MARKET_DATA_ACCESS_TOKEN or UPSTOX_ACCESS_TOKEN in the environment/.env.",
        )
    try:
        client_kwargs: dict = {"timeout": timeout, "follow_redirects": False}
        if transport is None:
            # Keep certificate and hostname verification enabled. The spec ships
            # this certifi bundle into frozen builds; test transports bypass TLS.
            client_kwargs["verify"] = _SSL_CONTEXT
        async with httpx.AsyncClient(transport=transport, **client_kwargs) as client:
            response = await client.get(
                authorize_url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.TimeoutException as exc:
        return result(
            "timeout", None, False,
            f"Connection to Upstox timed out after {timeout:g}s ({type(exc).__name__}); "
            "check network latency / firewall and retry. This is not a credential verdict.",
        )
    except TimeoutError as exc:
        return result(
            "timeout", None, False,
            f"Connection to Upstox timed out ({type(exc).__name__}); check network "
            "latency / firewall and retry. This is not a credential verdict.",
        )
    except httpx.ConnectError as exc:
        if _has_tls_error(exc):
            return result(
                "tls_failure", None, False,
                "TLS certificate/handshake validation failed; verify the PC clock, "
                "certifi CA bundle, and any proxy or antivirus HTTPS inspection. "
                "The credential was not evaluated.",
            )
        return result(
            "dns_or_connect_failure", None, False,
            "Could not reach Upstox (DNS/TCP connection failed); verify internet "
            "connectivity, proxy settings, and firewall. This is a network failure, "
            "not a credential failure.",
        )
    except ssl.SSLError as exc:
        return result(
            "tls_failure", None, False,
            "TLS certificate/handshake validation failed; verify the PC clock, "
            "certifi CA bundle, and any proxy or antivirus HTTPS inspection. "
            "The credential was not evaluated.",
        )
    except httpx.RemoteProtocolError as exc:
        if _has_tls_error(exc):
            return result(
                "tls_failure", None, False,
                "TLS certificate/handshake validation failed; verify the PC clock, "
                "certifi CA bundle, and any proxy or antivirus HTTPS inspection. "
                "The credential was not evaluated.",
            )
        return result(
            "protocol_failure", None, False,
            f"Upstox returned an invalid HTTPS protocol response ({type(exc).__name__}); "
            "check proxy/firewall behavior. The credential was not evaluated.",
        )
    except OSError as exc:
        if _has_tls_error(exc):
            return result(
                "tls_failure", None, False,
                "TLS certificate/handshake validation failed; verify the PC clock, "
                "certifi CA bundle, and any proxy or antivirus HTTPS inspection. "
                "The credential was not evaluated.",
            )
        return result(
            "dns_or_connect_failure", None, False,
            "Could not establish the HTTPS connection to Upstox (DNS/TCP failure); "
            "verify internet connectivity, proxy settings, and firewall. "
            "The credential was not evaluated.",
        )
    except httpx.HTTPError as exc:
        if _has_tls_error(exc):
            return result(
                "tls_failure", None, False,
                "TLS certificate/handshake validation failed; verify the PC clock, "
                "certifi CA bundle, and any proxy or antivirus HTTPS inspection. "
                "The credential was not evaluated.",
            )
        return result(
            "unreachable", None, False,
            f"Upstox authorize endpoint could not be reached ({type(exc).__name__}); "
            "connectivity failed before the credential could be evaluated.",
        )

    if response.status_code == 200:
        try:
            body = response.json()
            uri = body["data"]["authorized_redirect_uri"] if isinstance(body, dict) else None
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            uri = None
        if _is_upstox_websocket_uri(uri):
            return result(
                "feed_ok", 200, True,
                "Credential is authorized for Market Data Feed V3 via the documented "
                "GET authorize probe. The WebSocket has not yet been tested.",
                uri,
            )
        return result(
            "unexpected_response", 200, False,
            "Upstox returned HTTP 200 but the response body or secure Upstox "
            "authorized_redirect_uri was malformed; refusing to connect.",
        )
    if response.status_code == 401:
        return result("rejected", 401, False, GUIDANCE_REJECTED)
    if response.status_code == 403:
        return result("forbidden", 403, False, GUIDANCE_FORBIDDEN)
    return result(
        f"http_{response.status_code}", response.status_code, False,
        f"Upstox authorize returned HTTP {response.status_code}; the HTTPS endpoint "
        "was reached, but this response does not prove credential authorization. "
        "See the official documentation before retrying.",
    )


async def verify_feed_credential(
    token: str,
    *,
    authorize_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 20.0,
) -> CredentialCheck:
    """Verify (read-only) that this credential is authorized for feed V3.

    Performs the documented GET authorize probe — nothing else, no WebSocket
    connection, no order endpoint, and the token is never logged or returned.
    """
    outcome = await _probe_feed_credential(
        token, authorize_url=authorize_url, transport=transport, timeout=timeout,
    )
    return outcome.check


def build_login_url(client_id: str, redirect_uri: str, state: str | None = None) -> str:
    """Step 1 dialog URL. client_id is the public API key (safe to display)."""
    if not client_id or not redirect_uri:
        raise UpstoxOAuthError("client_id and redirect_uri are required")
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri}
    if state:
        params["state"] = state
    return f"{LOGIN_DIALOG_URL}?{urlencode(params)}"


def exchange_code_for_token(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 20.0,
) -> str:
    """Step 3 code→token exchange; returns the access token (never logged)."""
    if not all([code, client_id, client_secret, redirect_uri]):
        raise UpstoxOAuthError("code, client_id, client_secret and redirect_uri are required")
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    headers = {"Accept": "application/json"}
    try:
        client_kwargs: dict = {"timeout": timeout}
        if transport is None:
            client_kwargs["verify"] = _SSL_CONTEXT
        if transport is not None:  # test seam
            with httpx.Client(transport=transport, **client_kwargs) as client:
                response = client.post(TOKEN_URL, data=data, headers=headers)
        else:
            response = httpx.post(TOKEN_URL, data=data, headers=headers, **client_kwargs)
    except httpx.TimeoutException:
        raise UpstoxOAuthError(
            "Upstox token endpoint timed out; check network connectivity and retry."
        )
    except httpx.ConnectError:
        raise UpstoxOAuthError(
            "Could not reach Upstox (DNS/TCP connection failed); verify internet "
            "connectivity and firewall settings."
        )
    except (httpx.RemoteProtocolError, ssl.SSLError):
        raise UpstoxOAuthError(
            "TLS handshake with Upstox failed; check that local TLS/proxy/antivirus "
            "interception is not rewriting the Upstox certificate."
        )
    except httpx.HTTPError:
        raise UpstoxOAuthError("Upstox token endpoint unreachable (network error)")
    if response.status_code != 200:
        # Documented failure shape: {"status":"error","errors":[...]}; never echo it.
        raise UpstoxOAuthError(f"Upstox token exchange failed with HTTP {response.status_code}")
    try:
        token = response.json()["access_token"]
    except (KeyError, TypeError, ValueError):
        raise UpstoxOAuthError("Upstox token response missing access_token")
    if not isinstance(token, str) or not token:
        raise UpstoxOAuthError("Upstox token response invalid")
    return token


def store_token_in_env(token: str, env_path: Path) -> str:
    """Persist UPSTOX_ACCESS_TOKEN into the gitignored .env (chmod 600).

    Returns a human-readable outcome that never contains the token itself.
    """
    lines = []
    if env_path.exists():
        # utf-8 both ways, always. The operator's .env is hand-editable and can
        # contain any comment they like; a locale-encoded read raises
        # UnicodeDecodeError on Windows for a single non-ASCII character, and
        # a locale-encoded write would corrupt what it did not fail on. This is
        # the daily token refresh, so a code-page accident here costs a
        # trading day.
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                continue
            lines.append(line)
    lines.append(f"UPSTOX_ACCESS_TOKEN={token}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # POSIX honours this; Windows chmod can only toggle the read-only bit, so
    # the file keeps its inherited ACLs (owner-only under %LOCALAPPDATA%).
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    if os.name == "nt":
        return (f"UPSTOX_ACCESS_TOKEN written to {env_path} "
                "(file permissions: inherited Windows ACLs)")
    return f"UPSTOX_ACCESS_TOKEN written to {env_path} (file permissions 600)"


def run_login_flow(env_path: Path | None = None, *, transport=None, input=None, capture=None) -> str:
    """Interactive authorization-code flow: print dialog URL, capture code, exchange.

    `capture` (test seam) may inject a callable returning (code, state).
    `input` is injectable for deterministic tests. Secrets are only handled in
    memory or written to the gitignored .env — never stdout/logs.
    """
    env_path = env_path or Path.cwd() / ".env"
    client_id = os.getenv("UPSTOX_CLIENT_ID", "")
    client_secret = os.getenv("UPSTOX_CLIENT_SECRET", "")
    redirect_uri = os.getenv("UPSTOX_REDIRECT_URI", "")
    if not client_id or not client_secret or not redirect_uri:
        raise UpstoxOAuthError(
            "Set UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET and UPSTOX_REDIRECT_URI in the environment first"
        )
    print("Open this URL in a browser and log in on Upstox:")
    print(build_login_url(client_id, redirect_uri))  # public API key only, no secrets
    if capture is not None:
        code, _state = capture()
    else:
        pasted = (input or getpass.getpass)(
            "Paste the full redirect URL (or just the code; input hidden): "
        ).strip()
        if "=" in pasted:
            query = parse_qs(urlsplit(pasted).query)
            code = query.get("code", [""])[0]
        else:
            code = pasted
        if not code:
            raise UpstoxOAuthError("No authorization code provided")
    token = exchange_code_for_token(
        code, client_id, client_secret, redirect_uri, transport=transport
    )
    outcome = store_token_in_env(token, env_path)
    print(outcome)
    print("Token stored in gitignored .env; restart the bot to use it. It is never logged.")
    return "stored"
