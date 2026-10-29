"""Protected credential store for read-only Alpaca US market data.

Kept strictly separate from ``ALPACA_BROKER_PAPER`` and ``ALPACA_BROKER_LIVE``.
Market-data credentials authenticate read-only quotes, snapshots, bars, and
asset metadata only; they never authorize order submission.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from app.config.settings import Settings
from app.vantage.secrets import DpapiSecretStore

SLOT_ALPACA_MARKET_DATA = "ALPACA_MARKET_DATA"
VALID_FEEDS = frozenset({"iex", "sip", "delayed_sip"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_payload(api_key: str, api_secret: str, feed: str = "iex") -> dict[str, str]:
    key = (api_key or "").strip()
    secret = (api_secret or "").strip()
    feed_norm = (feed or "iex").strip().lower()
    if not key or not secret:
        raise ValueError("Both Alpaca Market Data API Key and API Secret are required")
    if len(key) > 256 or len(secret) > 256 or any(ch in key + secret for ch in "\r\n\x00"):
        raise ValueError("Invalid Alpaca Market Data credential format")
    if feed_norm not in VALID_FEEDS:
        raise ValueError("Feed must be one of: iex, sip, delayed_sip")
    return {"api_key": key, "api_secret": secret, "feed": feed_norm, "saved_at": _utc_now()}


class MemoryAlpacaDataStore:
    backend = "memory"

    def __init__(self) -> None:
        self._payload: dict[str, str] | None = None

    def save(self, api_key: str, api_secret: str, feed: str = "iex") -> dict[str, Any]:
        self._payload = _validate_payload(api_key, api_secret, feed)
        return self.status()

    def load(self) -> dict[str, str] | None:
        return dict(self._payload) if self._payload else None

    def delete(self) -> bool:
        existed = self._payload is not None
        self._payload = None
        return existed

    def status(self) -> dict[str, Any]:
        return {
            "slot": SLOT_ALPACA_MARKET_DATA,
            "present": self._payload is not None,
            "feed": self._payload["feed"] if self._payload else None,
            "saved_at": self._payload["saved_at"] if self._payload else None,
            "backend": self.backend,
            "secret_returned": False,
        }


class DpapiAlpacaDataStore:
    backend = "windows-dpapi"

    def __init__(self, directory: Path) -> None:
        self._inner = DpapiSecretStore(directory)

    def save(self, api_key: str, api_secret: str, feed: str = "iex") -> dict[str, Any]:
        payload = _validate_payload(api_key, api_secret, feed)
        self._inner.put(SLOT_ALPACA_MARKET_DATA, payload)
        return self.status()

    def load(self) -> dict[str, str] | None:
        raw = self._inner.get(SLOT_ALPACA_MARKET_DATA)
        return dict(raw) if isinstance(raw, dict) and raw.get("api_key") else None

    def delete(self) -> bool:
        return bool(self._inner.delete(SLOT_ALPACA_MARKET_DATA))

    def status(self) -> dict[str, Any]:
        raw = self.load()
        return {
            "slot": SLOT_ALPACA_MARKET_DATA,
            "present": raw is not None,
            "feed": raw.get("feed") if raw else None,
            "saved_at": raw.get("saved_at") if raw else None,
            "backend": self.backend,
            "secret_returned": False,
        }


class FileAlpacaDataStore:
    backend = "owner-0600-file"

    def __init__(self, directory: Path) -> None:
        self._path = Path(directory) / "alpaca-market-data.secret.json"

    def save(self, api_key: str, api_secret: str, feed: str = "iex") -> dict[str, Any]:
        payload = _validate_payload(api_key, api_secret, feed)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        finally:
            if tmp.exists():
                os.chmod(tmp, 0o600)
        tmp.replace(self._path)
        os.chmod(self._path, 0o600)
        return self.status()

    def load(self) -> dict[str, str] | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("api_key") or not data.get("api_secret"):
            return None
        return {str(k): str(v) for k, v in data.items() if v is not None}

    def delete(self) -> bool:
        if not self._path.exists():
            return False
        self._path.unlink(missing_ok=True)
        return True

    def status(self) -> dict[str, Any]:
        raw = self.load()
        return {
            "slot": SLOT_ALPACA_MARKET_DATA,
            "present": raw is not None,
            "feed": raw.get("feed") if raw else None,
            "saved_at": raw.get("saved_at") if raw else None,
            "backend": self.backend,
            "secret_returned": False,
        }


def default_alpaca_data_store(data_dir: Path | None):
    if data_dir is None:
        return MemoryAlpacaDataStore()
    secrets_dir = Path(data_dir) / "secrets"
    if os.name == "nt":
        return DpapiAlpacaDataStore(secrets_dir)
    return FileAlpacaDataStore(secrets_dir)


def resolve_alpaca_data_credentials(
    store: Any | None,
    settings: Settings | None = None,
) -> tuple[dict[str, str], str]:
    """Resolve read-only Alpaca Market Data keys from protected store, then env/Settings."""
    if store is not None:
        loaded = store.load()
        if loaded and loaded.get("api_key") and loaded.get("api_secret"):
            return (
                {
                    "api_key": loaded["api_key"],
                    "api_secret": loaded["api_secret"],
                    "feed": loaded.get("feed") or (settings.alpaca_data_feed if settings else "iex"),
                },
                "protected-store",
            )
    key = (settings.alpaca_api_key if settings else "") or os.getenv("ALPACA_API_KEY", "")
    secret = (settings.alpaca_api_secret if settings else "") or os.getenv("ALPACA_API_SECRET", "")
    feed = (settings.alpaca_data_feed if settings else "") or os.getenv("ALPACA_DATA_FEED", "iex")
    if key.strip() and secret.strip():
        return (
            {
                "api_key": key.strip(),
                "api_secret": secret.strip(),
                "feed": feed.strip().lower() if feed.strip().lower() in VALID_FEEDS else "iex",
            },
            "environment",
        )
    return (
        {
            "api_key": "",
            "api_secret": "",
            "feed": feed.strip().lower() if feed.strip().lower() in VALID_FEEDS else "iex",
        },
        "none",
    )
