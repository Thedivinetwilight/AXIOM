"""Secret redaction for cleanup logs and reports.

The cleanup log records paths, sizes, categories and reasons — never file
contents. As defence in depth every line written to the log passes through
``redact`` first, so even an unlucky path or error string that embeds a
credential-looking value is scrubbed before it reaches disk.

The patterns are deliberately blunt about VALUES but never about paths: a
cleanup log whose paths are redacted is useless, and a restore manifest built
from redacted paths is dangerous. Path-like strings (anything containing a
separator) are therefore left intact; only credential-shaped values are
replaced.
"""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"

# key=value / key: value where the key looks secret-bearing.
_KEY_VALUE_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_\-]*(?:token|secret|api[_\-]?key|apikey|password|passwd|"
    r"auth|authorization|credential|private[_\-]?key|access[_\-]?key|session)"
    r"[A-Za-z0-9_\-]*)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)

# Authorization: Bearer <jwt-ish>  /  bare JWT-shaped strings.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]{8,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9\-_]{4,}\.[A-Za-z0-9\-_]{4,}\.[A-Za-z0-9\-_]{4,}")

# AWS-style / Upstox-style prefixed keys.
_PREFIXED_RE = re.compile(r"\b(?:AKIA|ASIA|up_live_|up_test_)[A-Za-z0-9]{8,}\b")


def _looks_like_path(value: str) -> bool:
    value = value.strip().strip("\"'")
    return "/" in value or "\\" in value


def redact(text: str) -> str:
    """Return ``text`` with credential-shaped values replaced."""
    def replace_pair(match: re.Match[str]) -> str:
        if _looks_like_path(match.group(3)):
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{REDACTED}"

    out = _KEY_VALUE_RE.sub(replace_pair, text)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _JWT_RE.sub(REDACTED, out)
    out = _PREFIXED_RE.sub(REDACTED, out)
    return out


def looks_secret(name: str) -> bool:
    """True when a FILE NAME suggests it stores credentials."""
    lowered = name.lower()
    needles = ("token", "secret", "credential", "apikey", "api_key", "private",
               "keystore", "keyring", "auth")
    if any(needle in lowered for needle in needles):
        return True
    return lowered.startswith(".env.") and lowered not in (".env.example",)
