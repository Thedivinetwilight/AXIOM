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
import os
import stat

import httpx

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
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.get(
                authorize_url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return CredentialCheck(
            "unreachable", None, False,
            "Upstox authorize endpoint unreachable from this network; connectivity, "
            "not the credential, failed first.",
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
    return CredentialCheck(
        f"http_{response.status_code}", response.status_code, False,
        f"Upstox authorize returned HTTP {response.status_code}; see the official "
        "documentation before retrying.",
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
        if transport is not None:  # test seam
            with httpx.Client(transport=transport, timeout=timeout) as client:
                response = client.post(TOKEN_URL, data=data, headers=headers)
        else:
            response = httpx.post(TOKEN_URL, data=data, headers=headers, timeout=timeout)
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
        for line in env_path.read_text().splitlines():
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                continue
            lines.append(line)
    lines.append(f"UPSTOX_ACCESS_TOKEN={token}")
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only
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
