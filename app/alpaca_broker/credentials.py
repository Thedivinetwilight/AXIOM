"""Protected storage for Alpaca BROKER credentials (kept separate from market data).

Broker credentials never share a namespace with market-data keys
(``ALPACA_API_KEY``/``ALPACA_API_SECRET`` are market data ONLY). The store keys
are ``ALPACA_BROKER_PAPER`` and ``ALPACA_BROKER_LIVE`` — a credential saved for
one environment can never be read for the other.

Protection: Windows DPAPI (current user) following ``app/vantage/secrets.py``;
on POSIX an owner-only (0600) file inside the private data directory, clearly
labelled as file-permission protection. ``MemoryBrokerSecretStore`` exists for
tests only and is never selected automatically. Values are never logged or
repr'd.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path

BROKER_KEYS = ("ALPACA_BROKER_PAPER", "ALPACA_BROKER_LIVE")


class BrokerSecretStoreUnavailable(RuntimeError):
    pass


class BrokerSecretNotFound(RuntimeError):
    pass


def _validate_key(key: str) -> str:
    if key not in BROKER_KEYS:
        raise ValueError("broker credential key must be ALPACA_BROKER_PAPER or ALPACA_BROKER_LIVE")
    return key


class MemoryBrokerSecretStore:
    """Tests only. Never selected automatically by the application."""

    def __init__(self):
        self._values: dict[str, dict] = {}

    def put(self, key: str, value: dict) -> None:
        self._values[_validate_key(key)] = dict(value)

    def get(self, key: str) -> dict:
        _validate_key(key)
        if key not in self._values:
            raise BrokerSecretNotFound(f"no stored broker credential for {key}")
        return dict(self._values[key])

    def delete(self, key: str) -> None:
        self._values.pop(_validate_key(key), None)


class DpapiBrokerSecretStore:
    """Per-user DPAPI-encrypted broker credential files (Windows)."""

    def __init__(self, directory: Path):
        if os.name != "nt":
            raise BrokerSecretStoreUnavailable("DPAPI is only available on Windows")
        from app.vantage.secrets import DpapiSecretStore

        self._inner = DpapiSecretStore(Path(directory), allowed_keys=set(BROKER_KEYS))

    def put(self, key: str, value: dict) -> None:
        self._inner.put(_validate_key(key), value)

    def get(self, key: str) -> dict:
        return self._inner.get(_validate_key(key))

    def delete(self, key: str) -> None:
        self._inner.delete(_validate_key(key))


class FileBrokerSecretStore:
    """POSIX fallback: owner-only (0600) JSON files, explicit non-DPAPI."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{_validate_key(key).lower()}.broker-credential.json"

    def put(self, key: str, value: dict) -> None:
        path = self._path(key)
        path.write_text(json.dumps(dict(value), separators=(",", ":")), encoding="utf-8")
        os.chmod(path, 0o600)

    def get(self, key: str) -> dict:
        path = self._path(key)
        if not path.is_file():
            raise BrokerSecretNotFound(f"no stored broker credential for {key}")
        return json.loads(path.read_text(encoding="utf-8"))

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()


def default_store(directory: Path):
    """Production selection: DPAPI on Windows, owner-only files elsewhere."""
    if os.name == "nt":
        return DpapiBrokerSecretStore(directory)
    return FileBrokerSecretStore(directory)


def load_broker_credentials(store, environment: str, settings=None) -> tuple[dict, str]:
    """Resolve broker credentials for ONE environment.

    Order: protected store, then explicit environment variables (headless/CI).
    Returns ``(credentials, source)`` where source is ``protected-store``,
    ``environment`` or ``none``. Market-data credentials are never returned.
    """
    key = "ALPACA_BROKER_PAPER" if environment == "paper" else "ALPACA_BROKER_LIVE"
    store_miss = False
    try:
        value = store.get(key)
        api_key = str(value.get("api_key") or "")
        api_secret = str(value.get("api_secret") or "")
        if api_key and api_secret:
            return {"api_key": api_key, "api_secret": api_secret}, "protected-store"
        store_miss = True
    except (BrokerSecretNotFound, BrokerSecretStoreUnavailable, ValueError):
        store_miss = True  # explicit fallthrough to the environment source below
    if settings is not None:
        api_key = str(getattr(settings, "alpaca_broker_api_key", "") or "")
        api_secret = str(getattr(settings, "alpaca_broker_api_secret", "") or "")
        if api_key and api_secret:
            return {"api_key": api_key, "api_secret": api_secret}, "environment"
    return {"api_key": "", "api_secret": ""}, "none"


def _main() -> None:  # pragma: no cover - interactive CLI
    """Store a broker credential locally: python -m app.alpaca_broker.credentials"""
    import argparse

    parser = argparse.ArgumentParser(description="Store Alpaca broker credentials (protected)")
    parser.add_argument("environment", choices=["paper", "live"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    store = default_store(args.directory)
    key = "ALPACA_BROKER_PAPER" if args.environment == "paper" else "ALPACA_BROKER_LIVE"
    api_key = getpass.getpass("Alpaca broker API key (input hidden): ").strip()
    api_secret = getpass.getpass("Alpaca broker API secret (input hidden): ").strip()
    if not api_key or not api_secret:
        raise SystemExit("both values are required; nothing stored")
    store.put(key, {"api_key": api_key, "api_secret": api_secret})
    print(f"stored broker credential for {args.environment} ({key}) in {args.directory}")


if __name__ == "__main__":  # pragma: no cover
    _main()
