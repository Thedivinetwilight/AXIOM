"""OS-protected Vantage credentials and defensive redaction.

The production store uses Windows DPAPI (CryptProtectData, current user).  It
never falls back to plaintext.  ``MemorySecretStore`` exists only for isolated
unit/fake-terminal tests and is never selected automatically by the app.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class SecretStoreUnavailable(RuntimeError):
    pass


class SecretNotFound(RuntimeError):
    pass


class DATA_BLOB(ctypes.Structure):  # pragma: no cover - exercised on Windows
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, Any]:  # pragma: no cover - Windows
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


class DpapiSecretStore:
    """Per-user DPAPI encrypted files beneath AXIOM's private data directory."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        if os.name != "nt":
            raise SecretStoreUnavailable(
                "Windows DPAPI is required for Vantage credentials; no plaintext fallback exists"
            )

    def _path(self, key: str) -> Path:
        if key not in {"VANTAGE_DEMO", "VANTAGE_LIVE"}:
            raise ValueError("Invalid credential environment")
        return self.directory / f"{key.lower()}.dpapi"

    def put(self, key: str, value: dict) -> None:
        if os.name != "nt":  # defensive even if construction is bypassed
            raise SecretStoreUnavailable("DPAPI is only available on Windows")
        plaintext = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
        source, source_buffer = _blob(plaintext)
        encrypted = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        # Explicit Win64 signatures are required: ctypes' default int argument
        # conversion can otherwise truncate the HLOCAL returned by DPAPI.
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN: never open an unexpected OS credential prompt
        # inside the API process. The operator entered the secret in AXIOM already.
        if not crypt32.CryptProtectData(
            ctypes.byref(source), "AXIOM Vantage MT5 credential", None, None, None,
            0x01, ctypes.byref(encrypted),
        ):
            raise SecretStoreUnavailable("Windows DPAPI could not protect the credential")
        try:
            payload = ctypes.string_at(encrypted.pbData, encrypted.cbData)
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            temporary = path.with_suffix(".tmp")
            temporary.unlink(missing_ok=True)  # recover from an interrupted earlier write
            with open(temporary, "xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                # DPAPI still protects the ciphertext. Keep the write atomic,
                # but make a filesystem permission failure visible safely.
                logger.warning("Could not apply restrictive mode to a DPAPI credential file")
        finally:
            if encrypted.pbData:
                kernel32.LocalFree(encrypted.pbData)
        # Keep buffers alive through the OS call; then drop our references.
        del source_buffer, plaintext

    def get(self, key: str) -> dict:
        if os.name != "nt":
            raise SecretStoreUnavailable("DPAPI is only available on Windows")
        path = self._path(key)
        if not path.is_file():
            raise SecretNotFound("Vantage credential is not configured")
        encrypted_bytes = path.read_bytes()
        source, source_buffer = _blob(encrypted_bytes)
        plaintext = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        if not crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(plaintext),
        ):
            raise SecretStoreUnavailable(
                "Windows DPAPI could not decrypt this credential for the current user"
            )
        try:
            value = json.loads(ctypes.string_at(plaintext.pbData, plaintext.cbData).decode("utf-8"))
            if not isinstance(value, dict):
                raise SecretStoreUnavailable("Protected credential is invalid")
            return value
        finally:
            if plaintext.pbData:
                kernel32.LocalFree(plaintext.pbData)
            del source_buffer, encrypted_bytes

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class MemorySecretStore:
    """Explicit test double. Never use for packaged/operator credentials."""

    def __init__(self):
        self.values: dict[str, dict] = {}

    def put(self, key: str, value: dict) -> None:
        self.values[key] = dict(value)

    def get(self, key: str) -> dict:
        if key not in self.values:
            raise SecretNotFound("Vantage credential is not configured")
        return dict(self.values[key])

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.values


SENSITIVE_KEYS = {
    "password", "pass", "secret", "token", "api_token", "authorization",
    "apikey", "api_key", "access_token", "ciphertext", "credential", "credentials",
}


def redact(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Recursively redact sensitive fields and any accidentally included secret.

    This is applied before audit persistence and public error responses. It is
    deliberately conservative: a sensitive key becomes ``[REDACTED]`` regardless
    of its value type.
    """
    secrets = tuple(item for item in secrets if item)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in SENSITIVE_KEYS or any(
                marker in normalized for marker in ("password", "secret", "token")
            ):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact(item, secrets=secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        output = value
        for secret in secrets:
            output = output.replace(secret, "[REDACTED]")
        return output
    return value
