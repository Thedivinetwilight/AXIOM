"""Legacy compatibility shim for isolated Vantage credential tests.

Canonical OS-protected DPAPI storage now lives in ``app.security.dpapi_secrets``
so active Upstox (NSE) and Alpaca (NASDAQ) credential stores have zero runtime
imports from ``app.vantage``.
"""

from __future__ import annotations

from pathlib import Path

from app.security.dpapi_secrets import (
    DATA_BLOB,
    DpapiSecretStore as _NeutralDpapiSecretStore,
    MemorySecretStore,
    SENSITIVE_KEYS,
    SecretNotFound,
    SecretStoreUnavailable,
    redact,
)


class DpapiSecretStore(_NeutralDpapiSecretStore):
    """Legacy wrapper keeping default ``VANTAGE_DEMO``/``VANTAGE_LIVE`` keys for legacy tests."""

    def __init__(self, directory: Path, *, allowed_keys: set | None = None):
        keys = set(allowed_keys) if allowed_keys is not None else {"VANTAGE_DEMO", "VANTAGE_LIVE"}
        super().__init__(directory, allowed_keys=keys, label="AXIOM Vantage MT5 credential")


__all__ = [
    "DATA_BLOB",
    "DpapiSecretStore",
    "MemorySecretStore",
    "SENSITIVE_KEYS",
    "SecretNotFound",
    "SecretStoreUnavailable",
    "redact",
]
