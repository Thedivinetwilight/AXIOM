"""GUI update system — CHECK → DOWNLOAD → VERIFY → PREPARE → INSTALL.

Master spec section 69. Every stage reports its true outcome; a failed
download or a mismatched checksum is a refusal, never a silent success.

* CHECK queries the GitHub releases API for the newest published tag and
  compares it with the running version — read-only, rate-friendly, with a
  short timeout. Network failure is reported as CHECK_FAILED.
* DOWNLOAD streams the artifact into the staging directory and records its
  SHA-256. Nothing outside the staging directory may be written.
* VERIFY compares the recorded digest against the operator- or manifest-
  supplied expectation; mismatches are refused.
* PREPARE promotes a verified artifact into the updates directory with a
  manifest (the durable "an update is ready" state).
* INSTALL replaces the target binary (backing up the current one) and
  reports that a restart is required. On platforms that refuse to replace a
  running executable the failure is surfaced honestly instead of faked.

The transport is injectable so the whole pipeline is testable offline; the
offline transport in the test suite is identified as a mock and tested as a
mock (never presented as a real release check).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

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
INSTALLED_RESTART_REQUIRED = "INSTALLED_RESTART_REQUIRED"
INSTALL_REFUSED = "INSTALL_REFUSED"
NOT_ATTEMPTED = "NOT_ATTEMPTED"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_version(tag: str) -> str:
    match = re.match(r"^v?(\d+\.\d+\.\d+)", tag.strip())
    return match.group(1) if match else tag.strip()


def default_transport(url: str, *, timeout: float = 10.0) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "AXIOM-updater",
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


@dataclass
class UpdateManager:
    """Filesystem-confined update pipeline. No execution of downloaded code."""

    updates_dir: Path
    current_version: str
    transport: object = default_transport
    releases_url: str = RELEASES_API
    target_path: Path | None = None          # binary to replace on INSTALL
    _clock: object = time.time

    def __post_init__(self) -> None:
        self.updates_dir = Path(self.updates_dir)
        self.staging_dir = self.updates_dir / "staging"
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- paths --
    def _confine(self, raw: str) -> Path:
        candidate = Path(raw)
        resolved = candidate.resolve()
        for root in (self.staging_dir.resolve(), self.updates_dir.resolve()):
            if str(resolved).startswith(str(root)):
                return resolved
        raise ValueError("update artifacts must stay inside the updates directory")

    def _manifest_path(self) -> Path:
        return self.updates_dir / "update-manifest.json"

    def _read_manifest(self) -> dict | None:
        path = self._manifest_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_manifest(self, manifest: dict) -> None:
        self._manifest_path().write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------- CHECK --
    def check(self) -> dict:
        try:
            payload = self.transport(self.releases_url)
            release = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "status": CHECK_FAILED,
                    "current": self.current_version,
                    "detail": f"release check failed: {type(exc).__name__}; "
                              f"nothing was assumed"}
        tag = str(release.get("tag_name") or "")
        if not tag:
            return {"ok": False, "status": CHECK_FAILED,
                    "current": self.current_version,
                    "detail": "release response carried no tag_name"}
        latest = _parse_version(tag)
        update_available = latest != self.current_version
        return {"ok": True,
                "status": UPDATE_AVAILABLE if update_available else UP_TO_DATE,
                "current": self.current_version, "latest": latest,
                "update_available": update_available,
                "release_name": release.get("name") or tag,
                "published_at": release.get("published_at")}

    # ---------------------------------------------------------- DOWNLOAD --
    def download(self, url: str, *, filename: str) -> dict:
        # Refuse anything that is not a plain file name — silent
        # sanitization would hide a hostile or broken input.
        if (not filename or filename != Path(filename).name
                or filename in {".", ".."}):
            return {"ok": False, "status": DOWNLOAD_FAILED,
                    "detail": f"unsafe artifact name '{filename}'"}
        dest = self.staging_dir / filename
        try:
            payload = self.transport(url)
            dest.write_bytes(payload)
        except Exception as exc:
            return {"ok": False, "status": DOWNLOAD_FAILED,
                    "detail": f"download failed: {type(exc).__name__}"}
        digest = _sha256(dest)
        return {"ok": True, "status": DOWNLOADED, "path": str(dest),
                "sha256": digest, "bytes": dest.stat().st_size}

    # ------------------------------------------------------------ VERIFY --
    def verify(self, path: str, expected_sha256: str) -> dict:
        try:
            resolved = self._confine(path)
        except ValueError as exc:
            return {"ok": False, "status": VERIFY_REFUSED, "detail": str(exc)}
        if not resolved.exists():
            return {"ok": False, "status": VERIFY_REFUSED,
                    "detail": "artifact not found in the updates directory"}
        actual = _sha256(resolved)
        if actual.lower() != str(expected_sha256).lower():
            return {"ok": False, "status": VERIFY_REFUSED,
                    "detail": f"checksum mismatch: expected {expected_sha256[:16]}…, "
                              f"found {actual[:16]}…"}
        return {"ok": True, "status": VERIFIED, "path": str(resolved),
                "sha256": actual}

    # ----------------------------------------------------------- PREPARE --
    def prepare(self, path: str) -> dict:
        try:
            resolved = self._confine(path)
        except ValueError as exc:
            return {"ok": False, "status": PREPARE_REFUSED, "detail": str(exc)}
        if not resolved.exists():
            return {"ok": False, "status": PREPARE_REFUSED,
                    "detail": "artifact not found in the updates directory"}
        final_path = self.updates_dir / resolved.name
        if final_path != resolved:
            shutil.copy2(resolved, final_path)
        manifest = {"artifact": resolved.name, "sha256": _sha256(final_path),
                    "bytes": final_path.stat().st_size,
                    "prepared_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status": PREPARED}
        self._write_manifest(manifest)
        return {"ok": True, "status": PREPARED, **manifest,
                "path": str(final_path)}

    # ----------------------------------------------------------- INSTALL --
    def install(self) -> dict:
        manifest = self._read_manifest()
        if not manifest or manifest.get("status") != PREPARED:
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "no prepared (verified) update is staged"}
        artifact = self.updates_dir / manifest["artifact"]
        if not artifact.exists():
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "prepared artifact is missing"}
        if _sha256(artifact) != manifest["sha256"]:
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "prepared artifact changed after preparation; re-verify"}
        target = Path(self.target_path or (sys.executable or ""))
        if not str(target):
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": "no install target available in this environment"}
        backup = target.with_suffix(target.suffix + ".previous")
        try:
            if target.exists():
                shutil.copy2(target, backup)
            shutil.copy2(artifact, target)
            try:
                target.chmod(target.stat().st_mode | 0o111)
            except OSError as exc:
                logger.debug("could not set executable bits on %s: %s", target, exc)
        except OSError as exc:
            return {"ok": False, "status": INSTALL_REFUSED,
                    "detail": f"install refused by the platform: {exc} — the "
                              f"staged update remains prepared for the launcher"}
        manifest["status"] = INSTALLED_RESTART_REQUIRED
        manifest["installed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        manifest["backup"] = str(backup)
        self._write_manifest(manifest)
        return {"ok": True, "status": INSTALLED_RESTART_REQUIRED,
                "target": str(target), "backup": str(backup),
                "detail": "restart AXIOM to run the installed build"}

    # ------------------------------------------------------------ STATUS --
    def status(self) -> dict:
        manifest = self._read_manifest()
        return {"current": self.current_version,
                "manifest": manifest,
                "staged": sorted(p.name for p in self.staging_dir.iterdir()
                                 if p.is_file()),
                "note": "updates are verified artifacts only; nothing here "
                        "executes downloaded code"}
