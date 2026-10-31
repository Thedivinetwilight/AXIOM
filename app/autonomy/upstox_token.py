"""Manual Upstox access-token entry — protected per-user storage.

The operator pastes an Upstox access token (daily OAuth login token or an
Analytics Token) on the Daily Autonomy page. The value is stored encrypted for
the current Windows user through ``app/vantage/secrets.DpapiSecretStore`` —
the same DPAPI approach as the Vantage and Alpaca broker credentials. On
POSIX a clearly labelled owner-only (0600) file supports local development;
``MemoryUpstoxTokenStore`` exists for tests only and is never selected
automatically.

Contract (enforced by tests):
- The token is NEVER returned by status endpoints, never logged, never placed
  in a URL or query string, and never included in error messages.
- It is used ONLY for Upstox market data and required read-only account
  checks (``UpstoxReadOnly``: profile + quotes). That client has no order
  mutation methods at all.
- Saving, replacing, deleting or testing a token never creates a session,
  changes execution mode, Arms/Activate the app, or starts automation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

TOKEN_KEY = "UPSTOX_ACCESS_TOKEN"
# Live TRADING credentials are stored under a separate key and must never be
# silently reused from (or for) the read-only market-data token slot.
LIVE_TOKEN_KEY = "UPSTOX_LIVE_ACCESS_TOKEN"
MAX_TOKEN_LENGTH = 4096


class UpstoxTokenStoreUnavailable(RuntimeError):
    pass


class UpstoxTokenNotFound(RuntimeError):
    pass


def _validate_token(token: str) -> str:
    if not isinstance(token, str) or not token.strip():
        raise ValueError("an Upstox access token value is required")
    token = token.strip()
    if len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("the Upstox access token value is too long")
    return token


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryUpstoxTokenStore:
    """Tests only. Never selected automatically by the application."""

    def __init__(self, key: str = TOKEN_KEY):
        self._value: dict | None = None
        self._key = key

    def save(self, token: str, *, saved_at: str | None = None) -> dict:
        self._value = {"access_token": _validate_token(token),
                       "saved_at": saved_at or _now()}
        return self.status()

    def token(self) -> str:
        return str((self._value or {}).get("access_token") or "")

    def delete(self) -> bool:
        existed = self._value is not None
        self._value = None
        return existed

    def status(self) -> dict:
        value = self._value or {}
        return {
            "present": self._value is not None,
            "saved_at": value.get("saved_at"),
            "protection": "test-memory",
            "token_returned": False,
        }


class DpapiUpstoxTokenStore:
    """Per-user DPAPI-encrypted token file (Windows). No plaintext fallback."""

    def __init__(self, directory: Path, key: str = TOKEN_KEY):
        if os.name != "nt":
            raise UpstoxTokenStoreUnavailable("DPAPI is only available on Windows")
        from app.vantage.secrets import DpapiSecretStore

        self._inner = DpapiSecretStore(Path(directory), allowed_keys={key})
        self._key = key

    def save(self, token: str, *, saved_at: str | None = None) -> dict:
        self._inner.put(self._key, {"access_token": _validate_token(token),
                                    "saved_at": saved_at or _now()})
        return self.status()

    def token(self) -> str:
        from app.vantage.secrets import SecretNotFound

        try:
            value = self._inner.get(self._key)
        except SecretNotFound:
            raise UpstoxTokenNotFound("no stored Upstox access token") from None
        return _validate_token(str(value.get("access_token") or ""))

    def delete(self) -> bool:
        existed = self._inner.exists(self._key)
        self._inner.delete(self._key)
        return existed

    def status(self) -> dict:
        present = self._inner.exists(self._key)
        saved_at = None
        if present:
            try:
                saved_at = self._inner.get(self._key).get("saved_at")
            except Exception:  # noqa: BLE001 - status must never fail or leak
                saved_at = None
        return {"present": present, "saved_at": saved_at,
                "protection": "windows-dpapi-current-user", "token_returned": False}


class FileUpstoxTokenStore:
    """POSIX development fallback: owner-only (0600) file, explicitly not DPAPI."""

    def __init__(self, directory: Path, key: str = TOKEN_KEY):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._key = key

    def _path(self) -> Path:
        return self.directory / f"{self._key.lower()}.token.json"

    def save(self, token: str, *, saved_at: str | None = None) -> dict:
        path = self._path()
        path.write_text(json.dumps(
            {"access_token": _validate_token(token), "saved_at": saved_at or _now()},
            separators=(",", ":")), encoding="utf-8")
        os.chmod(path, 0o600)
        return self.status()

    def token(self) -> str:
        path = self._path()
        if not path.is_file():
            raise UpstoxTokenNotFound("no stored Upstox access token")
        return _validate_token(str(json.loads(path.read_text(encoding="utf-8")).get("access_token") or ""))

    def delete(self) -> bool:
        path = self._path()
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed

    def status(self) -> dict:
        present = self._path().is_file()
        saved_at = None
        if present:
            try:
                saved_at = json.loads(self._path().read_text(encoding="utf-8")).get("saved_at")
            except (OSError, ValueError):
                saved_at = None
        return {"present": present, "saved_at": saved_at,
                "protection": "owner-only-file-not-dpapi", "token_returned": False}


def default_store(directory: Path):
    """Production selection: DPAPI on Windows, owner-only files elsewhere."""
    if os.name == "nt":
        return DpapiUpstoxTokenStore(directory)
    return FileUpstoxTokenStore(directory)


def default_live_store(directory: Path):
    """Separate protected slot for the LIVE trading credential."""
    if os.name == "nt":
        return DpapiUpstoxTokenStore(directory, key=LIVE_TOKEN_KEY)
    return FileUpstoxTokenStore(directory, key=LIVE_TOKEN_KEY)


def resolve_token(store) -> tuple[str, str]:
    """Resolve the active token: protected store first, then the OAuth/CLI
    environment (``UPSTOX_ACCESS_TOKEN`` / ``MARKET_DATA_ACCESS_TOKEN``).

    Returns ``(token, source)`` with source ``protected-store``,
    ``environment`` or ``none``. The token itself is only for provider use and
    must never appear in a response, a log line or a URL.
    """
    if store is not None:
        try:
            value = store.token()
            if value:
                return value, "protected-store"
        except (UpstoxTokenNotFound, UpstoxTokenStoreUnavailable, ValueError):
            store_miss = True  # explicit fallthrough to the environment source
    environment = (os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
                   or os.getenv("MARKET_DATA_ACCESS_TOKEN", "").strip())
    if environment:
        return environment, "environment"
    return "", "none"


def resolve_token_provider(store):
    """``token_provider`` chain for AutonomyService: store, then environment."""
    def provider() -> str:
        token, _ = resolve_token(store)
        return token
    return provider


def resolve_live_token(store) -> tuple[str, str]:
    """Resolve the LIVE trading credential: protected store, then the explicit
    ``UPSTOX_LIVE_ACCESS_TOKEN`` environment variable ONLY.

    The read-only market-data token is NEVER returned here: live execution
    must never silently reuse a credential saved for read-only access.
    """
    if store is not None:
        try:
            value = store.token()
            if value:
                return value, "protected-store"
        except (UpstoxTokenNotFound, UpstoxTokenStoreUnavailable, ValueError):
            live_store_miss = True  # explicit fallthrough to the environment source
    environment = os.getenv("UPSTOX_LIVE_ACCESS_TOKEN", "").strip()
    if environment:
        return environment, "environment"
    return "", "none"


def resolve_live_token_provider(store):
    """``live_token_provider`` for AutonomyService: live slot, then the
    explicit live environment variable. Never the read-only chain."""
    def provider() -> str:
        token, _ = resolve_live_token(store)
        return token
    return provider
