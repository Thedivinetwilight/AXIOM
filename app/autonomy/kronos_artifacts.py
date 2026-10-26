"""Pinned Kronos artifact inspection and secure, atomic installation.

The reviewed manifest is the only source of repository/revision/file/hash/size
values.  URLs are constructed locally; the API never accepts a model URL.
Downloads use explicit HTTPS connections so every redirect host is checked.
A complete generation is verified before an atomic ``current.json`` switch.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import threading
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

MANIFEST_SCHEMA = "axiom.kronos.artifacts.v1"
CURRENT_SCHEMA = "axiom.kronos.current.v1"
# Official Hugging Face file/CDN hosts.  Any other redirect fails closed.
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "huggingface.co",
    "cas-bridge.xethub.hf.co",
    "cas-server.xethub.hf.co",
    "cas-server.xethub-eu.hf.co",
    "transfer.xethub.hf.co",
    "transfer.xethub-eu.hf.co",
    "us.aws.cdn.hf.co",
    "us.gcp.cdn.hf.co",
    "cdn-lfs-us-1.hf.co",
    "cdn-lfs-eu-1.hf.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co",
    "cdn-lfs-eu-1.huggingface.co",
})
_REPOSITORY = re.compile(r"NeoQuasar/Kronos-[A-Za-z0-9_.-]+\Z")
_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


class ArtifactError(RuntimeError):
    """Safe, operator-displayable artifact/download failure."""


class DownloadCancelled(ArtifactError):
    """The operator cancelled an artifact download."""


class DiskSpaceError(ArtifactError):
    """Insufficient space in the per-user artifact filesystem."""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(canonical(manifest).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ArtifactError("Kronos manifest contains an invalid relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or "/" in value:
        raise ArtifactError("Kronos manifest paths must be simple filenames")
    return value


def load_manifest(path: Path) -> dict:
    """Load and strictly validate the shipped artifact authority."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ArtifactError(f"Kronos manifest is unavailable: {type(exc).__name__}") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ArtifactError("Unsupported Kronos artifact manifest")
    source = manifest.get("source")
    if not isinstance(source, dict) or not _HEX_40.fullmatch(str(source.get("commit", ""))):
        raise ArtifactError("Kronos source commit is not pinned")
    if source.get("repository") != "https://github.com/shiyu-coder/Kronos":
        raise ArtifactError("Kronos source repository is not the reviewed official repository")
    if not isinstance(source.get("files"), dict) or not source["files"]:
        raise ArtifactError("Kronos source file hashes are missing")
    for relative, expected in source["files"].items():
        if not isinstance(relative, str) or relative.startswith(("/", "\\")) \
                or ".." in Path(relative).parts or not _HEX_64.fullmatch(str(expected)):
            raise ArtifactError("Kronos source file pins are invalid")
    for component in ("model", "tokenizer"):
        spec = manifest.get(component)
        if not isinstance(spec, dict):
            raise ArtifactError(f"Kronos {component} manifest is missing")
        if not _REPOSITORY.fullmatch(str(spec.get("repository", ""))):
            raise ArtifactError(f"Kronos {component} repository is not an official pinned repository")
        if not _HEX_40.fullmatch(str(spec.get("revision", ""))):
            raise ArtifactError(f"Kronos {component} revision is not a full commit pin")
        if spec.get("directory") != component:
            raise ArtifactError(f"Kronos {component} directory is invalid")
        files = spec.get("files")
        if not isinstance(files, dict) or not files:
            raise ArtifactError(f"Kronos {component} files are missing")
        for relative, expected in files.items():
            _safe_relative(relative)
            if not isinstance(expected, dict) \
                    or not _HEX_64.fullmatch(str(expected.get("sha256", ""))) \
                    or not isinstance(expected.get("size"), int) or expected["size"] <= 0:
                raise ArtifactError(f"Kronos {component}/{relative} integrity pin is invalid")
    if not isinstance(manifest["model"].get("context"), int) \
            or not 1 <= manifest["model"]["context"] <= 4096:
        raise ArtifactError("Kronos model context is invalid")
    return manifest


def expected_artifact_bytes(manifest: dict) -> int:
    return sum(
        expected["size"]
        for component in ("model", "tokenizer")
        for expected in manifest[component]["files"].values()
    )


def _file_view(path: Path, expected: dict, *, hash_files: bool) -> dict:
    view = {
        "expected_size": expected["size"], "expected_sha256": expected["sha256"],
        "present": path.is_file(), "actual_size": None, "actual_sha256": None,
        "verified": False, "issue": "",
    }
    if not path.is_file():
        view["issue"] = "MISSING"
        return view
    try:
        actual_size = path.stat().st_size
        view["actual_size"] = actual_size
        if actual_size != expected["size"]:
            view["issue"] = "SIZE_MISMATCH"
            return view
        if hash_files:
            actual = sha256_file(path)
            view["actual_sha256"] = actual
            if actual != expected["sha256"]:
                view["issue"] = "SHA256_MISMATCH"
                return view
        view["verified"] = True
    except OSError as exc:
        view["issue"] = f"READ_ERROR_{type(exc).__name__}"
    return view


def verify_source(manifest: dict, vendor_root: Path, *, hash_files: bool = True) -> dict:
    files = {}
    issues = []
    for relative, expected_hash in manifest["source"]["files"].items():
        path = vendor_root / relative
        expected = {"size": path.stat().st_size if path.is_file() else -1,
                    "sha256": expected_hash}
        view = _file_view(path, expected, hash_files=hash_files)
        # Source size is deliberately not part of the upstream manifest.
        if view["issue"] == "SIZE_MISMATCH":
            view["issue"] = ""
        view["expected_size"] = None
        files[relative] = view
        if not view["verified"]:
            issues.append(f"Vendored source mismatch: {relative}")
    return {
        "status": "VERIFIED" if not issues else "UNVERIFIED",
        "repository": manifest["source"]["repository"],
        "commit": manifest["source"]["commit"], "files": files,
        "issues": issues,
    }


class ArtifactStore:
    """Versioned per-user artifact trees with an atomic current pointer."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    def generation_path(self, digest: str) -> Path:
        return self.root / "generations" / digest

    def active_root(self, digest: str) -> tuple[Path, str]:
        try:
            current = json.loads(self.current_path.read_text(encoding="utf-8"))
            if current.get("schema") != CURRENT_SCHEMA \
                    or current.get("manifest_sha256") != digest \
                    or current.get("generation") != digest:
                raise ValueError("stale current pointer")
            candidate = self.generation_path(digest)
            if not candidate.is_dir():
                raise ValueError("generation missing")
            return candidate, "ATOMIC_GENERATION"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Backward compatibility for an operator who manually placed the
            # pinned files here before the installer existed.
            return self.root, "LEGACY_DIRECTORY"

    def partial_directories(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return [path for path in self.root.glob(".install-*") if path.is_dir()]

    def cleanup_partials(self) -> None:
        for path in self.partial_directories():
            if path.parent != self.root or path.is_symlink():
                continue
            shutil.rmtree(path)

    def inspect(self, manifest: dict, vendor_root: Path, *, hash_files: bool = True) -> dict:
        digest = manifest_digest(manifest)
        active, layout = self.active_root(digest)
        source = verify_source(manifest, vendor_root, hash_files=hash_files)
        result = {
            "manifest_sha256": digest, "layout": layout,
            "source": source, "issues": list(source["issues"]),
            "expected_total_bytes": expected_artifact_bytes(manifest),
            "partial_installs": len(self.partial_directories()),
        }
        verified = not result["issues"]
        for component in ("model", "tokenizer"):
            spec = manifest[component]
            files = {}
            for relative, expected in spec["files"].items():
                view = _file_view(active / component / relative, expected, hash_files=hash_files)
                files[relative] = view
                if not view["verified"]:
                    verified = False
                    result["issues"].append(f"{component}/{relative}: {view['issue']}")
            result[component] = {
                "repository": spec["repository"], "revision": spec["revision"],
                "expected_bytes": sum(item["size"] for item in spec["files"].values()),
                "files": files,
                "status": "VERIFIED" if all(item["verified"] for item in files.values())
                          else "UNAVAILABLE" if any(item["issue"] == "MISSING" for item in files.values())
                          else "UNVERIFIED",
            }
        result["artifact_verified"] = verified
        result["status"] = "VERIFIED" if verified else (
            "UNAVAILABLE" if any("MISSING" in issue for issue in result["issues"]) else "UNVERIFIED"
        )
        return result


class SecureHuggingFaceDownloader:
    """Small HTTPS downloader with an explicit per-redirect host policy."""

    def __init__(self, *, timeout: float = 20.0, max_redirects: int = 6):
        self.timeout = timeout
        self.max_redirects = max_redirects
        self._lock = threading.Lock()
        self._connection: http.client.HTTPSConnection | None = None

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                # Best-effort socket close during cancellation; later verification
                # still decides whether any artifact is usable.
                return

    @staticmethod
    def url_for(repository: str, revision: str, filename: str) -> str:
        return f"https://huggingface.co/{repository}/resolve/{revision}/{quote(filename)}"

    @staticmethod
    def _validate_url(url: str) -> tuple[str, str]:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise ArtifactError("Kronos download redirect has an invalid port") from None
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in ALLOWED_DOWNLOAD_HOSTS \
                or port not in {None, 443} or parsed.username or parsed.password:
            raise ArtifactError("Kronos download redirect left the explicit HTTPS host allowlist")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return host, path

    def _set_connection(self, connection):
        with self._lock:
            self._connection = connection

    def open(self, url: str, cancel) -> tuple[http.client.HTTPResponse, str]:
        current = url
        for _ in range(self.max_redirects + 1):
            if cancel.is_set():
                raise DownloadCancelled("Kronos download cancelled")
            host, path = self._validate_url(current)
            connection = http.client.HTTPSConnection(
                host, 443, timeout=self.timeout, context=ssl.create_default_context(),
            )
            self._set_connection(connection)
            try:
                connection.request("GET", path, headers={
                    "User-Agent": "AXIOM-Kronos-Pinned-Setup/1",
                    "Accept": "application/octet-stream", "Connection": "close",
                })
                response = connection.getresponse()
            except Exception as exc:
                self.close()
                if cancel.is_set():
                    raise DownloadCancelled("Kronos download cancelled") from None
                raise ArtifactError(f"Pinned download connection failed: {type(exc).__name__}") from None
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.close()
                self.close()
                if not location:
                    raise ArtifactError("Kronos download redirect had no destination")
                current = urljoin(current, location)  # never displayed or logged
                continue
            if response.status != 200:
                status = response.status
                response.close()
                self.close()
                raise ArtifactError(f"Pinned download returned HTTP {status} on {host}")
            return response, host
        raise ArtifactError("Too many pinned-download redirects")


class KronosArtifactInstaller:
    def __init__(self, manifest_path: Path, store: ArtifactStore,
                 downloader: SecureHuggingFaceDownloader | None = None):
        self.manifest_path = Path(manifest_path)
        self.store = store
        self.downloader = downloader or SecureHuggingFaceDownloader()

    def install(self, *, cancel: threading.Event, progress: Callable[[dict], None]) -> dict:
        manifest = load_manifest(self.manifest_path)
        digest = manifest_digest(manifest)
        total = expected_artifact_bytes(manifest)
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.store.cleanup_partials()
        free = shutil.disk_usage(self.store.root).free
        required = total + 16 * 1024 * 1024
        if free < required:
            raise DiskSpaceError("Insufficient disk space for the pinned Kronos artifacts")
        staging = self.store.root / f".install-{uuid4().hex}"
        completed = 0
        try:
            for component in ("model", "tokenizer"):
                spec = manifest[component]
                for relative, expected in spec["files"].items():
                    if cancel.is_set():
                        raise DownloadCancelled("Kronos download cancelled")
                    target = staging / component / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    partial = target.with_name(target.name + ".part")
                    url = self.downloader.url_for(spec["repository"], spec["revision"], relative)
                    progress({"phase": "DOWNLOAD", "component": component, "file": relative,
                              "bytes": completed, "total_bytes": total, "file_bytes": 0,
                              "file_total_bytes": expected["size"]})
                    response, _host = self.downloader.open(url, cancel)
                    digestor = hashlib.sha256()
                    written = 0
                    try:
                        content_length = response.getheader("Content-Length")
                        if content_length is not None and int(content_length) != expected["size"]:
                            raise ArtifactError(f"Pinned size header mismatch: {component}/{relative}")
                        with partial.open("xb") as handle:
                            while True:
                                if cancel.is_set():
                                    raise DownloadCancelled("Kronos download cancelled")
                                chunk = response.read(1024 * 1024)
                                if not chunk:
                                    break
                                written += len(chunk)
                                if written > expected["size"]:
                                    raise ArtifactError(f"Pinned file exceeded expected size: {component}/{relative}")
                                digestor.update(chunk)
                                handle.write(chunk)
                                progress({"phase": "DOWNLOAD", "component": component, "file": relative,
                                          "bytes": completed + written, "total_bytes": total,
                                          "file_bytes": written, "file_total_bytes": expected["size"]})
                            handle.flush()
                            os.fsync(handle.fileno())
                    except DownloadCancelled:
                        raise
                    except ArtifactError:
                        raise
                    except Exception as exc:
                        if cancel.is_set():
                            raise DownloadCancelled("Kronos download cancelled") from None
                        raise ArtifactError(f"Pinned download interrupted: {type(exc).__name__}") from None
                    finally:
                        response.close()
                        self.downloader.close()
                    if written != expected["size"]:
                        raise ArtifactError(f"Pinned size mismatch: {component}/{relative}")
                    if digestor.hexdigest() != expected["sha256"]:
                        raise ArtifactError(f"Pinned SHA-256 mismatch: {component}/{relative}")
                    partial.replace(target)
                    completed += written
            # Verify the complete staged generation independently before publish.
            for component in ("model", "tokenizer"):
                for relative, expected in manifest[component]["files"].items():
                    view = _file_view(staging / component / relative, expected, hash_files=True)
                    if not view["verified"]:
                        raise ArtifactError(f"Staged verification failed: {component}/{relative}")
            generation = self.store.generation_path(digest)
            generation.parent.mkdir(parents=True, exist_ok=True)
            preserve = False
            if generation.is_dir() and not generation.is_symlink():
                # A valid active generation is never deleted just because the
                # operator retried setup.  The newly staged copy can be discarded.
                preserve = all(
                    _file_view(generation / component / relative, expected, hash_files=True)["verified"]
                    for component in ("model", "tokenizer")
                    for relative, expected in manifest[component]["files"].items()
                )
            if generation.exists() and not preserve:
                # The old generation was already invalid; retain it under a
                # private installer name until the complete replacement exists.
                replaced = self.store.root / f".replaced-{uuid4().hex}"
                generation.replace(replaced)
                try:
                    staging.replace(generation)
                except Exception:
                    replaced.replace(generation)
                    raise
                if replaced.is_dir() and not replaced.is_symlink():
                    shutil.rmtree(replaced, ignore_errors=True)
                else:
                    replaced.unlink(missing_ok=True)
            elif not preserve:
                staging.replace(generation)
            pointer = self.store.root / f".current-{uuid4().hex}.tmp"
            pointer.write_text(canonical({
                "schema": CURRENT_SCHEMA, "manifest_sha256": digest, "generation": digest,
            }), encoding="utf-8")
            # Windows requires a writable handle for fsync, unlike POSIX.
            with pointer.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(pointer, self.store.current_path)
            progress({"phase": "ARTIFACTS_VERIFIED", "bytes": total, "total_bytes": total,
                      "manifest_sha256": digest})
            return {"manifest_sha256": digest, "bytes": total}
        finally:
            self.downloader.close()
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
