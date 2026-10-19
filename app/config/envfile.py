"""Read and write the operator's .env file without destroying it.

Two rules drive this module:

1. **Never lose a line the operator wrote.** Comments, blank lines, ordering
   and unrelated keys are preserved; only the keys being changed are touched.
   Someone hand-editing their risk limits should not have that work erased
   because they also changed a symbol through a setup screen.

2. **Never echo a secret.** Nothing here logs, prints or returns key values in
   an error. Writing is atomic (temp file + replace) so an interrupted save
   cannot leave a half-written configuration behind, and the file is created
   with owner-only permissions where the platform supports it.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_KEY_RE = re.compile(r"^(?P<lead>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>\s*=\s*)(?P<rest>.*)$")


def _split_value(raw: str) -> str:
    """Extract the value from everything after `KEY=`.

    A comment marker only ends the value when it appears OUTSIDE quotes, so
    ``API_TOKEN="abc #def"`` keeps its hash instead of being truncated.
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in {'"', "'"}:
        quote = raw[0]
        out: list[str] = []
        index = 1
        while index < len(raw):
            char = raw[index]
            if char == "\\" and index + 1 < len(raw):
                out.append(raw[index + 1])
                index += 2
                continue
            if char == quote:
                return "".join(out)
            out.append(char)
            index += 1
        return "".join(out)  # unterminated quote: take the rest as-is
    # unquoted: an inline comment starts at " #" (a bare # inside a token such
    # as a URL fragment is kept)
    for marker in (" #", "\t#"):
        if marker in raw:
            raw = raw.split(marker, 1)[0]
    return raw.strip()


def read_values(path: Path) -> dict[str, str]:
    """Parse a .env into a mapping. Never raises on a malformed line."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_RE.match(line)
        if not match:
            continue
        values[match.group("key")] = _split_value(match.group("rest"))
    return values


def render_value(value: str) -> str:
    """Quote a value only when the dotenv parser would otherwise misread it."""
    if value == "":
        return ""
    needs_quotes = any(ch in value for ch in ' #"\'$') or value != value.strip()
    if needs_quotes:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _secure_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def update_values(path: Path, updates: dict[str, str]) -> dict:
    """Set keys in a .env, preserving everything else.

    Returns a summary safe to show a user: which keys changed and which were
    added. Values are never included.
    """
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    applied: list[str] = []
    added: list[str] = []
    remaining = dict(updates)

    out: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        match = _KEY_RE.match(line)
        if not match:
            out.append(line)
            continue
        key = match.group("key")
        if key in remaining:
            new_value = remaining.pop(key)
            out.append(f"{match.group('lead')}{key}={render_value(new_value)}")
            applied.append(key)
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        if any(line.strip().startswith("#") for line in out):
            out.append("# Added by the Arena setup screen")
        for key, value in remaining.items():
            out.append(f"{key}={render_value(value)}")
            added.append(key)

    text = "\n".join(out).rstrip("\n") + "\n"
    _secure_write(path, text)

    return {
        "path": str(path),
        "updated": sorted(applied),
        "added": sorted(added),
        "changed_count": len(applied) + len(added),
    }


def restore_snapshot(path: Path, content: bytes | None) -> None:
    """Restore a `.env` to its exact pre-write bytes (or delete it).

    `content=None` means the file did not exist before the attempted write, so
    a refused change must not leave an empty file behind. Used by the wizard's
    rollback path, where "nothing was saved" must be literally true.
    """
    path = Path(path)
    if content is None:
        path.unlink(missing_ok=True)
        return
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def remove_values(path: Path, keys: list[str]) -> dict:
    """Delete keys from a .env, preserving every other line.

    Used to roll back a setup write that could not be applied completely, so a
    refused change leaves the configuration exactly as it was. Returns the keys
    actually removed (values are never included).
    """
    targets = set(keys)
    if not path.exists():
        return {"path": str(path), "removed": []}
    existing_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    removed: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        match = _KEY_RE.match(line)
        if match and match.group("key") in targets:
            removed.append(match.group("key"))
            continue
        out.append(line)
    if removed:
        text = "\n".join(out).rstrip("\n") + "\n"
        _secure_write(path, text)
    return {"path": str(path), "removed": sorted(set(removed))}


def masked_summary(path: Path, keys: list[str]) -> dict[str, bool]:
    """Which of `keys` are present and non-empty. Presence only, no values."""
    values = read_values(path)
    return {key: bool((values.get(key) or "").strip()) for key in keys}
