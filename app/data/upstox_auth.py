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

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import getpass
import hmac
import os
import secrets
import ssl

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
    "Credential not authorized for Market Data Feed V3 (HTTP 401). Do not assume "
    "an Analytics Token works for the V3 feed: the current analytics-token docs "
    "list a 'Websocket' category among supported read-only APIs, but access must "
    "be verified behaviorally with your own token. A standard OAuth login access "
    "token (python -m app upstox-login) is the documented path for the feed; "
    "regenerate your token if it expired."
)
GUIDANCE_FORBIDDEN = (
    "Credential refused for Market Data Feed V3 (HTTP 403). If this is an "
    "Analytics Token, websocket/feed access depends on current Upstox policy and "
    "account state — the documented safe path for the V3 market-data feed is a "
    "standard OAuth login access token. Check the official analytics-token and "
    "authorize pages before retrying."
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
    now = datetime.now(timezone.utc).isoformat()
    if not isinstance(token, str) or not token.strip():
        return CredentialCheck(
            "invalid_input", None, False,
            "No credential provided. Set MARKET_DATA_ACCESS_TOKEN or UPSTOX_ACCESS_TOKEN "
            "in the environment/.env.",
            now,
        )
    try:
        client_kwargs: dict = {"timeout": timeout}
        if transport is None:
            # Only pin the trust store when using a real network transport;
            # unit tests inject their own _StaticTransport which does not
            # accept `verify`.
            client_kwargs["verify"] = _SSL_CONTEXT
        async with httpx.AsyncClient(transport=transport, **client_kwargs) as client:
            response = await client.get(
                authorize_url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.TimeoutException as exc:
        return CredentialCheck(
            "timeout", None, False,
            f"Connection to Upstox timed out after {timeout:g}s ({type(exc).__name__}); "
            "check network latency / firewall and retry.",
            now,
        )
    except httpx.ConnectError as exc:
        return CredentialCheck(
            "dns_or_connect_failure", None, False,
            "Could not reach Upstox (DNS/TCP connection failed); verify internet "
            "connectivity, proxy settings, and firewall. This is a network failure, "
            "not a credential failure.",
            now,
        )
    except (httpx.RemoteProtocolError, ssl.SSLError) as exc:
        return CredentialCheck(
            "tls_failure", None, False,
            f"TLS handshake failed ({type(exc).__name__}); check that local TLS / "
            "proxy / antivirus interception is not rewriting the Upstox certificate. "
            "This is not a credential failure.",
            now,
        )
    except httpx.HTTPError as exc:
        return CredentialCheck(
            "unreachable", None, False,
            f"Upstox authorize endpoint unreachable from this network "
            f"({type(exc).__name__}); connectivity, not the credential, failed first.",
            now,
        )
    if response.status_code == 200:
        try:
            uri = response.json()["data"]["authorized_redirect_uri"]
        except (KeyError, TypeError, ValueError):
            uri = None
        if isinstance(uri, str) and uri.startswith("wss://"):
            return CredentialCheck(
                "feed_ok", 200, True,
                "Credential is authorized for Market Data Feed V3 (verified via the "
                "documented GET authorize probe). Safe to open the WebSocket.",
                now,
            )
        return CredentialCheck(
            "unexpected_response", 200, False,
            "Authorize returned 200 but an unexpected body shape; refusing to "
            "connect. The official response must carry data.authorized_redirect_uri.",
            now,
        )
    if response.status_code == 401:
        return CredentialCheck("rejected", 401, False, GUIDANCE_REJECTED, now)
    if response.status_code == 403:
        return CredentialCheck("forbidden", 403, False, GUIDANCE_FORBIDDEN, now)
    if response.status_code == 429:
        retry_after = (response.headers.get("Retry-After") or "").strip()
        wait = f" Retry after {retry_after}." if retry_after.isdigit() else ""
        return CredentialCheck(
            "http_429", 429, False,
            "Upstox rate-limited the authorize request (HTTP 429). This is not "
            "a credential verdict; wait and retry." + wait,
            now,
        )
    if 500 <= response.status_code <= 599:
        return CredentialCheck(
            f"http_{response.status_code}", response.status_code, False,
            f"Upstox returned HTTP {response.status_code} from the authorize "
            "endpoint (their service error). The credential was not judged; "
            "retry later.",
            now,
        )
    if response.status_code == 400:
        return CredentialCheck(
            "http_400", 400, False,
            "Upstox rejected the authorize request as malformed (HTTP 400). "
            "Check MARKET_DATA_AUTHORIZE_URL (it must be the documented "
            "V3 feed authorize endpoint) and that the token is current.",
            now,
        )
    return CredentialCheck(
        f"http_{response.status_code}", response.status_code, False,
        f"Upstox authorize returned HTTP {response.status_code}; see the official "
        "documentation before retrying. This is not proof the credential is valid.",
        now,
    )


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


#: Both names are read by Settings.from_env (the MARKET_DATA_* spelling is
#: the documented primary, UPSTOX_ACCESS_TOKEN the V0.6.2 alias). A refresh
#: must update EVERY token key already present: writing only the alias while
#: the primary remained in .env meant `upstox-login` reported success but the
#: engine kept using yesterday's expired token — the reported "not connecting
#: to Upstox" failure.
TOKEN_ENV_KEYS = ("MARKET_DATA_ACCESS_TOKEN", "UPSTOX_ACCESS_TOKEN")


def store_token_in_env(token: str, env_path: Path) -> str:
    """Persist a refreshed access token into the gitignored .env (chmod 600).

    Updates every token variable already configured so a stale duplicate can
    never shadow the new credential. The write is atomic and preserves every
    unrelated line/comment. Returns a human-readable outcome that never
    contains the token itself.
    """
    from app.config.envfile import read_values, update_values

    env_path = Path(env_path)
    existing = read_values(env_path) if env_path.exists() else {}
    keys = [key for key in TOKEN_ENV_KEYS if key in existing] or [
        "UPSTOX_ACCESS_TOKEN"]
    update_values(env_path, {key: token for key in keys})
    # update_values writes atomically and sets owner-only modes on POSIX.
    # Windows chmod can only toggle the read-only bit, so the file keeps its
    # inherited ACLs (owner-only under %LOCALAPPDATA%).
    if os.name == "nt":
        return (f"{', '.join(keys)} written to {env_path} "
                "(file permissions: inherited Windows ACLs)")
    return f"{', '.join(keys)} written to {env_path} (file permissions 600)"


def run_login_flow(env_path: Path | None = None, *, transport=None, input=None, capture=None) -> str:
    """Interactive authorization-code flow: print dialog URL, capture code, exchange.

    `capture` (test seam) may inject a callable returning (code, state).
    `input` is injectable for deterministic tests. Secrets are only handled in
    memory or written to the gitignored .env — never stdout/logs.
    """
    env_path = env_path or Path.cwd() / ".env"
    # The setup screen writes these three values to the SAME .env the token is
    # stored in. Reading only the process environment meant a correctly
    # configured .env produced "Set UPSTOX_CLIENT_ID ... first" — one of the
    # paths behind "not connecting to Upstox". The file is read here, and an
    # exported variable still wins, so a shell override keeps working.
    file_values: dict[str, str] = {}
    if env_path.exists():
        # This used to import python-dotenv and silently `pass` when the
        # optional dependency was missing from a packaged build, which turned
        # a perfectly good .env into "Set UPSTOX_CLIENT_ID ... first". Read
        # the file with Arena's own parser; an exported variable still wins.
        # Nothing is written into os.environ: the values are passed to the
        # exchange explicitly, so a long-lived process cannot inherit them.
        from app.config.envfile import read_values

        try:
            file_values = read_values(env_path)
        except OSError as exc:
            raise UpstoxOAuthError(
                f"Could not read {env_path} ({type(exc).__name__}); fix the file "
                "or export UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET and "
                "UPSTOX_REDIRECT_URI in the environment."
            ) from None

    def _setting(name: str) -> str:
        return os.getenv(name, "") or file_values.get(name, "")

    client_id = _setting("UPSTOX_CLIENT_ID")
    client_secret = _setting("UPSTOX_CLIENT_SECRET")
    redirect_uri = _setting("UPSTOX_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        raise UpstoxOAuthError(
            "Set UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET and UPSTOX_REDIRECT_URI first "
            f"(environment or {env_path})"
        )
    # OAuth 2.0 state: a value we generate and verify when the redirect URL is
    # returned. It binds the authorization response to this login attempt.
    state = secrets.token_urlsafe(18)
    print("Open this URL in a browser and log in on Upstox:")
    print(build_login_url(client_id, redirect_uri, state))  # public API key only
    if capture is not None:
        code, returned_state = capture()
    else:
        pasted = (input or getpass.getpass)(
            "Paste the full redirect URL (recommended — it verifies the "
            "request) or just the code (input hidden): "
        ).strip()
        if "=" in pasted:
            query = parse_qs(urlsplit(pasted).query)
            code = query.get("code", [""])[0]
            returned_state = query.get("state", [None])[0]
        else:
            code = pasted
            returned_state = None
        if not code:
            raise UpstoxOAuthError("No authorization code provided")
    if returned_state is not None and not hmac.compare_digest(
            str(returned_state), state):
        # Never exchange a code that did not come back from our own request.
        raise UpstoxOAuthError(
            "OAuth state did not match this login attempt; the authorization "
            "response was rejected and no token was requested. Start again."
        )
    token = exchange_code_for_token(
        code, client_id, client_secret, redirect_uri, transport=transport
    )
    outcome = store_token_in_env(token, env_path)
    print(outcome)
    print("Token stored in gitignored .env; restart the bot to use it. It is never logged.")
    return "stored"
