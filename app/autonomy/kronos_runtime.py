"""Per-user, locked CPU runtime provisioning for the offline Kronos worker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from app.autonomy.kronos_artifacts import (
    ArtifactError, DiskSpaceError, DownloadCancelled, canonical,
)

_RUNTIME_SCHEMA = "axiom.kronos.runtime.v1"
_PACKAGE = re.compile(
    r"(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[A-Za-z0-9_.+!-]+)"
    r"(?:;\s*(?P<marker>sys_platform == \"win32\"|python_version >= \"3\.12\"))?\Z"
)
_ALLOWED_INDEXES = {
    "https://pypi.org/simple",
    "https://download.pytorch.org/whl/cpu",
}


def runtime_lock(path: Path) -> dict:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ArtifactError(f"Kronos runtime lock is unavailable: {type(exc).__name__}") from None
    packages = []
    indexes = []
    for source_line in raw.decode("utf-8").splitlines():
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--index-url ") or line.startswith("--extra-index-url "):
            url = line.split(" ", 1)[1].strip()
            if url not in _ALLOWED_INDEXES or urlsplit(url).scheme != "https":
                raise ArtifactError("Kronos runtime lock contains an unapproved package index")
            indexes.append(url)
            continue
        match = _PACKAGE.fullmatch(line)
        if not match:
            raise ArtifactError("Kronos runtime lock must contain only exact pinned packages")
        values = match.groupdict()
        values["normalized_name"] = values["name"].lower().replace("_", "-")
        packages.append(values)
    if not packages or set(indexes) != _ALLOWED_INDEXES:
        raise ArtifactError("Kronos runtime lock is incomplete")
    return {
        "sha256": hashlib.sha256(raw).hexdigest(), "packages": packages,
        "indexes": indexes, "path": str(path),
    }


def applicable_packages(lock: dict) -> list[dict]:
    result = []
    for item in lock["packages"]:
        marker = item["marker"]
        if marker == 'sys_platform == "win32"' and sys.platform != "win32":
            continue
        if marker == 'python_version >= "3.12"' and sys.version_info[:2] < (3, 12):
            continue
        result.append(item)
    return result


def _environment(temp: Path | None = None) -> dict:
    allowed = (
        "PATH", "SYSTEMROOT", "WINDIR", "LD_LIBRARY_PATH", "NUMBER_OF_PROCESSORS",
    )
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    env.update({
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    })
    if temp is not None:
        temp.mkdir(parents=True, exist_ok=True)
        env.update({"TMPDIR": str(temp), "TEMP": str(temp), "TMP": str(temp)})
    return env


class KronosRuntimeManager:
    """Install and invoke only the locked per-user CPU runtime."""

    def __init__(self, artifact_root: Path, lock_path: Path, worker_path: Path, *,
                 application_root: Path, python_command: list[str] | None = None):
        self.artifact_root = Path(artifact_root)
        self.lock_path = Path(lock_path)
        self.worker_path = Path(worker_path)
        self.application_root = Path(application_root)
        self.python_command = python_command
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._python: dict | None = None

    @staticmethod
    def required_free_bytes() -> int:
        # Conservative download + unpacked CPU wheel/temp allowance.  The exact
        # artifact sizes come from the model manifest, not this runtime budget.
        return 4 * 1024 ** 3

    def install_plan(self) -> dict:
        lock = runtime_lock(self.lock_path)
        return {
            "python": "CPython 3.11 or 3.12 x64 with pip",
            "device": "CPU only", "cuda_stack": False,
            "lock_sha256": lock["sha256"], "indexes": lock["indexes"],
            "packages": [{"name": item["name"], "version": item["version"],
                          "marker": item["marker"]} for item in lock["packages"]],
            "minimum_free_bytes": self.required_free_bytes(),
            "location": "AXIOM per-user data directory",
        }

    def _candidates(self) -> list[list[str]]:
        if self.python_command:
            return [list(self.python_command)]
        candidates = []
        if not getattr(sys, "frozen", False) and sys.version_info[:2] in {(3, 11), (3, 12)}:
            candidates.append([sys.executable])
        if sys.platform == "win32":
            candidates.extend([["py", "-3.11"], ["py", "-3.12"], ["python"]])
        else:
            candidates.extend([["python3.11"], ["python3.12"], ["python3"], ["python"]])
        result = []
        for command in candidates:
            if command and command[0] and command not in result:
                result.append(command)
        return result

    def _python_probe(self, command: list[str]) -> dict | None:
        code = (
            "import json,platform,sys,pip;print(json.dumps({"
            "'version':list(sys.version_info[:3]),'machine':platform.machine(),"
            "'system':platform.system(),'executable':sys.executable,'pip':pip.__version__}))"
        )
        try:
            result = subprocess.run(
                [*command, "-I", "-c", code], capture_output=True, text=True,
                timeout=20, env=_environment(), check=False,
            )
            if result.returncode:
                return None
            info = json.loads(result.stdout.strip())
            version = tuple(info["version"])
            if version[:2] not in {(3, 11), (3, 12)}:
                return None
            if info["machine"].lower() not in {"amd64", "x86_64"}:
                return None
            info["command"] = command
            return info
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return None

    def python(self, *, refresh: bool = False) -> dict | None:
        with self._lock:
            if self._python is not None and not refresh:
                return dict(self._python)
            for command in self._candidates():
                info = self._python_probe(command)
                if info is not None:
                    self._python = info
                    return dict(info)
            return None

    def runtime_dir(self, python: dict) -> Path:
        version = python["version"]
        key = "cp{}{}-{}-{}".format(
            version[0], version[1], python["system"].lower(), python["machine"].lower(),
        )
        key = re.sub(r"[^a-zA-Z0-9_.-]", "-", key)
        return self.artifact_root / "runtime" / key

    def _register(self, process):
        with self._lock:
            self._process = process

    def _clear(self, process):
        with self._lock:
            if self._process is process:
                self._process = None

    def cancel_active(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                # The subsequent runtime probe, not termination success alone,
                # determines whether inference remains available.
                return

    def _run(self, command: list[str], *, input_text: str | None = None,
             timeout: float, cancel: threading.Event | None = None,
             temp: Path | None = None) -> tuple[int, str, str]:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_environment(temp), shell=False,
        )
        self._register(process)
        started = time.monotonic()
        first_attempt = True
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    self.cancel_active()
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    process.kill()
                    process.communicate()
                    raise ArtifactError("Kronos runtime operation timed out")
                try:
                    stdout, stderr = process.communicate(
                        input=input_text if first_attempt else None,
                        timeout=min(1.0, remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    first_attempt = False
                    input_text = None
                    continue
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Kronos runtime operation cancelled")
            return process.returncode, stdout, stderr
        finally:
            self._clear(process)

    def _worker(self, python: dict, site: Path, *, payload: dict | None = None,
                timeout: float, cancel: threading.Event | None = None) -> dict:
        if not self.worker_path.is_file():
            raise ArtifactError("Kronos offline worker is missing from the application resources")
        command = [python["executable"], "-I", str(self.worker_path), "--runtime", str(site),
                   "--lock", str(self.lock_path), "--app-root", str(self.application_root)]
        if payload is None:
            command.append("--probe-only")
        code, stdout, _stderr = self._run(
            command, input_text=json.dumps(payload) if payload is not None else None,
            timeout=timeout, cancel=cancel,
        )
        try:
            result = json.loads(stdout)
        except (ValueError, TypeError):
            raise ArtifactError("Kronos worker returned no safe JSON result") from None
        if code != 0 or not isinstance(result, dict):
            raise ArtifactError("Kronos worker process failed")
        return result

    @staticmethod
    def _validate_probe(result: dict, lock: dict) -> None:
        expected = {item["normalized_name"]: item["version"] for item in applicable_packages(lock)}
        versions = result.get("package_versions") or {}
        normalized = {key.lower().replace("_", "-"): str(value) for key, value in versions.items()}
        if normalized != expected:
            raise ArtifactError("Installed Kronos runtime does not exactly match the lock")
        if result.get("device") != "cpu" or result.get("cuda_available") is not False \
                or result.get("torch_cuda_build") is not None:
            raise ArtifactError("Kronos runtime is not the required CPU-only build")
        if result.get("status") != "READY":
            raise ArtifactError("Kronos runtime probe failed")

    def probe(self, *, cancel: threading.Event | None = None) -> dict:
        python = self.python()
        base = {
            "status": "UNAVAILABLE", "verified": False, "loaded": False,
            "installed": False, "python": python, "issues": [], "device": "cpu",
        }
        if python is None:
            base["issues"] = ["CPython 3.11/3.12 x64 with pip is required for the optional runtime"]
            return base
        lock = runtime_lock(self.lock_path)
        directory = self.runtime_dir(python)
        marker_path = directory / "axiom-runtime.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("schema") != _RUNTIME_SCHEMA \
                    or marker.get("lock_sha256") != lock["sha256"] \
                    or marker.get("python_version") != python["version"]:
                raise ValueError("runtime identity mismatch")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            base["issues"] = ["Locked per-user runtime is not installed for this Python/platform"]
            return base
        base["installed"] = True
        try:
            result = self._worker(python, directory / "site", timeout=90, cancel=cancel)
            self._validate_probe(result, lock)
            return {**base, **result, "status": "READY", "verified": True,
                    "python": python, "issues": [], "lock_sha256": lock["sha256"]}
        except (ArtifactError, OSError) as exc:
            return {**base, "status": "UNAVAILABLE", "issues": [str(exc)],
                    "lock_sha256": lock["sha256"]}

    def install(self, *, cancel: threading.Event,
                progress: Callable[[dict], None]) -> dict:
        python = self.python(refresh=True)
        if python is None:
            raise ArtifactError("CPython 3.11/3.12 x64 with pip is required for the optional runtime")
        lock = runtime_lock(self.lock_path)
        target = self.runtime_dir(python)
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < self.required_free_bytes():
            raise DiskSpaceError("Insufficient disk space for the locked Kronos CPU runtime")
        staging = target.parent / f".install-{uuid4().hex}"
        backup = None
        try:
            site = staging / "site"
            site.mkdir(parents=True)
            progress({"phase": "RUNTIME_INSTALL", "runtime_lock_sha256": lock["sha256"]})
            def requirement(item: dict) -> str:
                return item["name"] + "==" + item["version"] + (
                    "; " + item["marker"] if item["marker"] else ""
                )

            torch_items = [item for item in lock["packages"] if item["normalized_name"] == "torch"]
            if len(torch_items) != 1 or not torch_items[0]["version"].endswith("+cpu"):
                raise ArtifactError("Kronos runtime lock does not contain exactly one CPU Torch pin")
            common = [
                python["executable"], "-m", "pip", "install", "--isolated",
                "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                "--no-deps", "--only-binary=:all:", "--target", str(site),
            ]
            # Never let an extra index supply ordinary packages.  Conversely,
            # the +cpu Torch distribution can come only from the official CPU index.
            commands = [
                common + ["--index-url", "https://pypi.org/simple"] + [
                    requirement(item) for item in lock["packages"]
                    if item["normalized_name"] != "torch"
                ],
                common + ["--index-url", "https://download.pytorch.org/whl/cpu",
                          requirement(torch_items[0])],
            ]
            for command in commands:
                code, stdout, stderr = self._run(
                    command, timeout=1800, cancel=cancel, temp=target.parent / "tmp",
                )
                if code:
                    combined = (stdout + stderr).lower()
                    if "no space left on device" in combined or "disk quota" in combined:
                        raise DiskSpaceError("The runtime installer ran out of disk space")
                    raise ArtifactError("Locked CPU runtime installation failed; no fallback was installed")
            progress({"phase": "RUNTIME_PROBE", "runtime_lock_sha256": lock["sha256"]})
            result = self._worker(python, site, timeout=120, cancel=cancel)
            self._validate_probe(result, lock)
            (staging / "axiom-runtime.json").write_text(canonical({
                "schema": _RUNTIME_SCHEMA, "lock_sha256": lock["sha256"],
                "python_version": python["version"], "device": "cpu",
                "package_versions": result["package_versions"],
            }), encoding="utf-8")
            if target.exists():
                backup = target.parent / f".replaced-{uuid4().hex}"
                target.replace(backup)
            staging.replace(target)
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            progress({"phase": "RUNTIME_VERIFIED", "runtime_lock_sha256": lock["sha256"]})
            return self.probe(cancel=cancel)
        except Exception:
            if backup is not None and backup.exists() and not target.exists():
                backup.replace(target)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def forecast(self, payload: dict, *, timeout: float = 300,
                 cancel: threading.Event | None = None) -> dict:
        python = self.python()
        if python is None:
            raise ArtifactError("Kronos runtime Python is unavailable")
        directory = self.runtime_dir(python)
        return self._worker(
            python, directory / "site", payload=payload, timeout=timeout, cancel=cancel,
        )
