"""Filesystem-confined update system — CHECK → DOWNLOAD → VERIFY → PREPARE → INSTALL.

Every stage reports its true outcome. A download is only staged data: PREPARE
requires a successful checksum verification, and INSTALL re-hashes the prepared
artifact before replacing an explicitly configured application binary. Source
mode never guesses that ``sys.executable`` is the application and therefore
cannot overwrite the Python interpreter.

The transport is injectable for offline protocol tests. Its presence does not
weaken broker or application authorization boundaries; this module never
executes downloaded code.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

logger = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/Thedivinetwilight/AXIOM/releases/latest"
CHECKED = "CHECKED"
CHECK_FAILED = "CHECK_FAILED"
UP_TO_DATE = "UP_TO_DATE"
UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
DOWNLOADED = "DOWNLOADED"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
VERIFIED = "VERIFIED"
VERIFY_REFUSED = "VERIFY_REFUSED"
PREPARED = "PREPARED"
PREPARE_REFUSED = "PREPARE_REFUSED"
INSTALLING = "INSTALLING"
INSTALLED_RESTART_REQUIRED = "INSTALLED_RESTART_REQUIRED"
INSTALL_REFUSED = "INSTALL_REFUSED"
NOT_ATTEMPTED = "NOT_ATTEMPTED"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_version(tag: str) -> str:
    """Normalize a conventional vMAJOR.MINOR.PATCH tag; keep unknown tags raw."""
    value = str(tag).strip()
    match = _VERSION_RE.fullmatch(value)
    if not match:
        return value
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}" + (
        f"-{match.group(4)}" if match.group(4) else "")


def _version_parts(value: str):
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        return None
    core = tuple(int(match.group(index)) for index in (1, 2, 3))
    prerelease = match.group(4)
    identifiers = None
    if prerelease is not None:
        parts = prerelease.split(".")
        # SemVer forbids leading zeroes in numeric prerelease identifiers.
        if any(part.isdigit() and len(part) > 1 and part.startswith("0")
               for part in parts):
            return None
        identifiers = tuple(int(part) if part.isdigit() else part for part in parts)
    return core, identifiers


def _compare_versions(left: str, right: str) -> int | None:
    """SemVer precedence without a runtime dependency on ``packaging``."""
    parsed_left, parsed_right = _version_parts(left), _version_parts(right)
    if parsed_left is None or parsed_right is None:
        return None
    core_left, pre_left = parsed_left
    core_right, pre_right = parsed_right
    if core_left != core_right:
        return 1 if core_left > core_right else -1
    if pre_left is None or pre_right is None:
        if pre_left is pre_right:
            return 0
        return 1 if pre_left is None else -1
    for a, b in zip(pre_left, pre_right):
        if a == b:
            continue
        if isinstance(a, int) and isinstance(b, int):
            return 1 if a > b else -1
        if isinstance(a, int) != isinstance(b, int):
            return -1 if isinstance(a, int) else 1
        return 1 if a > b else -1
    if len(pre_left) == len(pre_right):
        return 0
    return -1 if len(pre_left) < len(pre_right) else 1


def _safe_download_url(value: str) -> bool:
    try:
        parts = urlsplit(str(value))
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
    except (TypeError, ValueError):
        return False
    if (parts.scheme.lower() != "https" or not host or parts.username is not None
            or parts.password is not None or port not in (None, 443)):
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


def default_transport(url: str, *, timeout: float = 10.0) -> bytes:
    if not _safe_download_url(url):
        raise ValueError("update URLs must use a public HTTPS host")
    request = urllib.request.Request(
        url, headers={"User-Agent": "AXIOM-updater",
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _safe_filename(filename: str) -> bool:
    return bool(filename and filename not in {".", ".."}
                and Path(filename).name == filename
                and "/" not in filename and "\\" not in filename
                and not filename.startswith(".."))


@dataclass
class UpdateManager:
    """Confined updater. No execution of downloaded code."""

    updates_dir: Path
    current_version: str
    transport: object = default_transport
    releases_url: str = RELEASES_API
    target_path: Path | None = None
    _clock: object = time.time
    _verified_artifacts: dict[str, str] = field(default_factory=dict,
                                                 init=False, repr=False)

    def __post_init__(self) -> None:
        self.updates_dir = Path(self.updates_dir)
        self.staging_dir = self.updates_dir / "staging"
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- paths --
    def _confine(self, raw: str | Path) -> Path:
        candidate = Path(raw).expanduser()
        resolved = candidate.resolve()
        root = self.updates_dir.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError("update artifacts must stay inside the updates directory") from None
        return resolved

    def _manifest_path(self) -> Path:
        return self.updates_dir / "update-manifest.json"

    def _read_manifest(self) -> dict | None:
        path = self._manifest_path()
        if not path.exists() or path.is_symlink() or not path.is_file():
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(manifest, dict):
            return None
        artifact = manifest.get("artifact")
        digest = manifest.get("sha256")
        size = manifest.get("bytes")
        if (type(manifest.get("schema")) is not int or manifest.get("schema") != 1
                or not _safe_filename(artifact) or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
                or type(size) is not int or size < 0
                or manifest.get("verification_basis")
                    != "caller-supplied-sha256-match"
                or manifest.get("release_authenticity_verified") is not False
                or manifest.get("status") not in {
                    PREPARED, INSTALLING, INSTALLED_RESTART_REQUIRED}):
            return None
        return manifest

    def _write_manifest(self, manifest: dict) -> None:
        path = self._manifest_path()
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with open(temp, "x", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    # ------------------------------------------------------------- CHECK --
    def check(self) -> dict:
        try:
            payload = self.transport(self.releases_url)
            release = json.loads(payload.decode("utf-8"))
            if not isinstance(release, dict):
                raise ValueError("release response is not an object")
            tag_value = release.get("tag_name")
            if not isinstance(tag_value, str) or not tag_value.strip():
                raise ValueError("release response carried no tag_name")
            latest = _parse_version(tag_value)
            current = _parse_version(self.current_version)
            comparison = _compare_versions(latest, current)
            if comparison is None:
                raise ValueError("current version or latest tag is not semantic MAJOR.MINOR.PATCH")
        except Exception as exc:
            return {"ok": False, "status": CHECK_FAILED,
                    "current": self.current_version,
                    "detail": f"release check failed: {type(exc).__name__}; "
                              "nothing was assumed"}
        update_available = comparison > 0
        result = {"ok": True,
                  "status": UPDATE_AVAILABLE if update_available else UP_TO_DATE,
                  "current": current, "latest": latest,
                  "update_available": update_available,
                  "release_name": release.get("name") or tag_value,
                  "published_at": release.get("published_at")}
        if comparison < 0:
            result["note"] = "published release is older than this build; downgrades are disabled"
        return result

    # ---------------------------------------------------------- DOWNLOAD --
    def download(self, url: str, *, filename: str) -> dict:
        if not _safe_filename(filename):
            return {"ok": False, "status": DOWNLOAD_FAILED,
                    "detail": f"unsafe artifact name '{filename}'"}
        if not _safe_download_url(url):
            return {"ok": False, "status": DOWNLOAD_FAILED,
                    "detail": "update URL must use a public HTTPS host"}
        dest = self.staging_dir / filename
        temp = dest.with_name(f".{dest.name}.{uuid4().hex}.tmp")
        try:
            payload = self.transport(url)
            if not isinstance(payload, (bytes, bytearray, memoryview)):
                raise TypeError("transport did not return bytes")
            with open(temp, "xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, dest)
        except Exception as exc:
            temp.unlink(missing_ok=True)
            return {"ok": False, "status": DOWNLOAD_FAILED,
                    "detail": f"download failed: {type(exc).__name__}"}
        self._verified_artifacts.pop(str(dest.resolve()), None)
        digest = _sha256(dest)
        return {"ok": True, "status": DOWNLOADED, "path": str(dest),
                "sha256": digest, "bytes": dest.stat().st_size,
                "digest_role": "locally-computed-only",
                "release_authenticity_verified": False,
                "note": "downloaded bytes have a local SHA-256; publisher authenticity "
                        "has not been verified"}

    # ------------------------------------------------------------ VERIFY --
    def verify(self, path: str, expected_sha256: str) -> dict:
        try:
            resolved = self._confine(path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "status": VERIFY_REFUSED, "detail": str(exc)}
        if not resolved.is_file():
            self._verified_artifacts.pop(str(resolved), None)
            return {"ok": False, "status": VERIFY_REFUSED,
                    "detail": "artifact not found as a regular file in the updates directory"}
        if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
            self._verified_artifacts.pop(str(resolved), None)
            return {"ok": False, "status": VERIFY_REFUSED,
                    "detail": "expected_sha256 must be exactly 64 hexadecimal characters"}
        actual = _sha256(resolved)
        if actual.lower() != expected_sha256.lower():
            self._verified_artifacts.pop(str(resolved), None)
            return {"ok": False, "status": VERIFY_REFUSED,
                    "detail": f"checksum mismatch: expected {expected_sha256[:16]}…, "
                              f"found {actual[:16]}…"}
        self._verified_artifacts[str(resolved)] = actual
        return {"ok": True, "status": VERIFIED, "path": str(resolved),
                "sha256": actual,
                "verification_basis": "caller-supplied-sha256-match",
                "release_authenticity_verified": False,
                "note": "the artifact matches the caller-supplied SHA-256; this is not "
                        "a signature or proof of publisher authenticity"}

    # ----------------------------------------------------------- PREPARE --
    def prepare(self, path: str) -> dict:
        try:
            resolved = self._confine(path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "status": PREPARE_REFUSED, "detail": str(exc)}
        if not resolved.is_file():
            return {"ok": False, "status": PREPARE_REFUSED,
                    "detail": "artifact not found in the updates directory"}
        verified_digest = self._verified_artifacts.get(str(resolved))
        if not verified_digest:
            return {"ok": False, "status": PREPARE_REFUSED,
                    "detail": "artifact must pass VERIFY in this process before PREPARE"}
        if _sha256(resolved) != verified_digest:
            self._verified_artifacts.pop(str(resolved), None)
            return {"ok": False, "status": PREPARE_REFUSED,
                    "detail": "artifact changed after verification; verify it again"}

        final_path = self.updates_dir / resolved.name
        if resolved != final_path.resolve():
            if final_path.is_symlink():
                return {"ok": False, "status": PREPARE_REFUSED,
                        "detail": "prepared artifact destination is a symlink"}
            temp = self.updates_dir / f".{resolved.name}.{uuid4().hex}.tmp"
            try:
                shutil.copy2(resolved, temp)
                if _sha256(temp) != verified_digest:
                    raise OSError("staged artifact changed while it was copied")
                os.replace(temp, final_path)
            except OSError as exc:
                temp.unlink(missing_ok=True)
                return {"ok": False, "status": PREPARE_REFUSED,
                        "detail": f"verified artifact could not be prepared ({type(exc).__name__})"}
        manifest = {"schema": 1, "artifact": final_path.name,
                    "sha256": verified_digest,
                    "bytes": final_path.stat().st_size,
                    "verification_basis": "caller-supplied-sha256-match",
                    "release_authenticity_verified": False,
                    "prepared_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status": PREPARED}
        try:
            self._write_manifest(manifest)
        except OSError as exc:
            return {"ok": False, "status": PREPARE_REFUSED,
                    "detail": f"verified artifact is staged but manifest write failed "
                              f"({type(exc).__name__})"}
        return {"ok": True, "status": PREPARED, **manifest,
                "path": str(final_path)}

    # ----------------------------------------------------------- INSTALL --
    def install(self) -> dict:
        manifest = self._read_manifest()
        if not manifest or manifest.get("status") != PREPARED:
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "no structurally valid prepared (verified) update is staged"}
        artifact = self.updates_dir / manifest["artifact"]
        if artifact.is_symlink() or not artifact.is_file():
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "prepared artifact is missing or is not a regular file"}
        try:
            if artifact.resolve().parent != self.updates_dir.resolve():
                raise ValueError("prepared artifact resolves outside the updates directory")
        except OSError as exc:
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": f"prepared artifact path is invalid ({type(exc).__name__})"}
        if (artifact.stat().st_size != manifest["bytes"]
                or _sha256(artifact) != manifest["sha256"]):
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "prepared artifact changed after preparation; re-verify"}
        if self.target_path is None:
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "no explicit frozen-application install target is configured; "
                              "source mode will not overwrite sys.executable"}

        target = Path(self.target_path).expanduser()
        if not target.is_file():
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "explicit install target is not an existing regular file"}
        backup = target.with_suffix(target.suffix + ".previous")
        temp_backup = backup.with_name(f".{backup.name}.{uuid4().hex}.tmp")
        temp_target = target.with_name(f".{target.name}.{uuid4().hex}.install")
        try:
            shutil.copy2(target, temp_backup)
            with open(temp_backup, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temp_backup, backup)
            shutil.copy2(artifact, temp_target)
            if _sha256(temp_target) != manifest["sha256"]:
                raise OSError("verified artifact changed during install staging")
            try:
                temp_target.chmod(temp_target.stat().st_mode | 0o111)
            except OSError as exc:
                logger.debug("could not set executable bits on %s: %s", temp_target, exc)
            os.replace(temp_target, target)
        except OSError as exc:
            temp_backup.unlink(missing_ok=True)
            temp_target.unlink(missing_ok=True)
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": f"install refused by the platform: {type(exc).__name__}; "
                              "the current target was not intentionally modified and "
                              "the staged update remains prepared"}

        manifest["status"] = INSTALLED_RESTART_REQUIRED
        manifest["installed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        manifest["backup"] = str(backup)
        try:
            self._write_manifest(manifest)
        except OSError as exc:
            # The atomic target replacement has already happened; say so and
            # do not misreport this as a successful manifest transition.
            return {"ok": False, "status": INSTALL_REFUSED,
                    "target_replaced": True, "target": str(target),
                    "backup": str(backup),
                    "detail": "binary was atomically replaced, but the update state "
                              f"manifest could not be updated ({type(exc).__name__}); "
                              "inspect the target before retrying"}
        return {"ok": True, "status": INSTALLED_RESTART_REQUIRED,
                "target": str(target), "backup": str(backup),
                "verification_basis": "caller-supplied-sha256-match",
                "release_authenticity_verified": False,
                "detail": "artifact matched the caller-supplied SHA-256; publisher "
                          "authenticity/signature was not verified. Restart AXIOM to "
                          "run the installed build"}

    # ------------------------------------------------------------ STATUS --
    def status(self) -> dict:
        manifest = self._read_manifest()
        return {"current": self.current_version,
                "manifest": manifest,
                "staged": sorted(p.name for p in self.staging_dir.iterdir()
                                 if p.is_file() and not p.is_symlink()),
                "release_authenticity_verified": False,
                "note": "checksum matching is against a caller-supplied SHA-256; "
                        "publisher authenticity/signature is not verified, and "
                        "nothing here executes downloaded code"}
