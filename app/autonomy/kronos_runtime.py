"""Per-user, locked CPU runtime provisioning for the offline Kronos worker."""

from __future__ import annotations

import contextlib
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
_SELECTION_SCHEMA = "axiom.kronos.python-selection.v1"
_PY_LAUNCHER_ROW = re.compile(
    r"^\s*-V:(?P<major>\d+)\.(?P<minor>\d+)(?P<arch>-32)?\s+"
    r"(?:\*\s*)?(?P<executable>.+?)\s*$"
)


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


def applicable_packages(lock: dict, *, python: dict | None = None) -> list[dict]:
    """Evaluate the two lock markers for the interpreter that will run Kronos.

    A frozen application's own Python can differ from the selected system
    interpreter, so parent-side probe validation must not use host defaults.
    """
    if python is None:
        version = sys.version_info[:2]
        platform_name = sys.platform
    else:
        version = tuple(python["version"][:2])
        system = str(python["system"]).lower()
        platform_name = {"windows": "win32", "darwin": "darwin"}.get(system, system)
    result = []
    for item in lock["packages"]:
        marker = item["marker"]
        if marker == 'sys_platform == "win32"' and platform_name != "win32":
            continue
        if marker == 'python_version >= "3.12"' and version < (3, 12):
            continue
        result.append(item)
    return result


def _environment(temp: Path | None = None) -> dict:
    allowed = (
        "PATH", "SYSTEMROOT", "WINDIR", "LD_LIBRARY_PATH", "NUMBER_OF_PROCESSORS",
        # CPython 3.11's Windows platform.machine() reads these non-secret
        # architecture identifiers; stripping them makes valid x64 look unknown.
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432",
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
        self.python_command = list(python_command) if python_command else None
        self._fixed_python_command = self.python_command is not None
        self._manual_python = False
        self._selection_path = self.artifact_root / "python-selection.json"
        if self.python_command is None:
            selected = self._read_python_selection()
            if selected:
                self.python_command = [selected]
                self._manual_python = True
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._python: dict | None = None
        self._candidate_sources: dict[tuple[str, ...], str] = {}
        self._python_diagnostics: list[str] = []
        self._last_probe_error = "Python identity probe was not run"

    def _read_python_selection(self) -> str | None:
        try:
            raw = json.loads(self._selection_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            if raw.get("schema") != _SELECTION_SCHEMA:
                return None
            executable = raw.get("executable")
            if isinstance(executable, str) and executable and len(executable) <= 1024:
                return executable
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return None

    def _write_python_selection(self, executable: str | None) -> None:
        temporary = self.artifact_root / f".python-selection-{uuid4().hex}.tmp"
        try:
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            temporary.write_text(canonical({
                "schema": _SELECTION_SCHEMA, "executable": executable,
            }), encoding="utf-8")
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, self._selection_path)
        except OSError:
            raise ArtifactError("Could not save the Kronos Python selection in per-user storage") from None
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def select_python(self, executable: str | None) -> dict | None:
        """Validate and prefer one Python executable, or restore auto-discovery."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ArtifactError("Python selection is locked while a Kronos runtime operation is active")
            text = (executable or "").strip()
            if not text:
                self._write_python_selection(None)
                self.python_command = None
                self._manual_python = False
                self._python = None
                return self.python(refresh=True)
            if len(text) > 1024 or "\x00" in text:
                raise ArtifactError("Select one full Python executable path without command-line arguments")
            if len(text) >= 2 and text[0] == text[-1] == '"':
                text = text[1:-1].strip()
            try:
                path = Path(text).expanduser()
                if not path.is_absolute() or not path.is_file():
                    raise ValueError("not an executable file")
                resolved = str(path.resolve(strict=True))
            except (OSError, ValueError, RuntimeError):
                raise ArtifactError(
                    "Select an existing full CPython 3.11/3.12 x64 executable path with pip; no arguments"
                ) from None
            info = self._python_probe([resolved])
            if info is None:
                raise ArtifactError(
                    "Selected Python is not CPython 3.11/3.12 x64 with pip: "
                    + self._last_probe_error
                )
            try:
                if not os.path.samefile(resolved, info["executable"]):
                    raise ValueError("launcher is not the selected executable")
            except (OSError, ValueError):
                raise ArtifactError(
                    "Select the CPython executable itself, not a launcher or command wrapper"
                ) from None
            self._write_python_selection(resolved)
            self.python_command = [resolved]
            self._manual_python = True
            info["selection"] = "manual"
            info["command_label"] = "manual selection"
            self._python = info
            return dict(info)

    def _py_launcher_candidates(self) -> list[list[str]]:
        """Enumerate registered x64 interpreters without invoking a shell."""
        try:
            result = subprocess.run(
                ["py", "-0p"], capture_output=True, text=True, timeout=10,
                env=_environment(), check=False, shell=False,
            )
            if result.returncode:
                return []
        except (OSError, subprocess.SubprocessError):
            return []
        candidates = []
        for line in result.stdout.splitlines():
            match = _PY_LAUNCHER_ROW.match(line)
            if not match:
                continue
            version = (int(match.group("major")), int(match.group("minor")))
            executable = match.group("executable").strip()
            if len(executable) >= 2 and executable[0] == executable[-1] == '"':
                executable = executable[1:-1].strip()
            if version in {(3, 11), (3, 12)} and match.group("arch") is None and executable:
                command = [executable]
                if command not in candidates:
                    candidates.append(command)
        return candidates

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
        candidates: list[list[str]] = []
        sources: dict[tuple[str, ...], str] = {}

        def add(command: list[str], label: str) -> None:
            if command and command[0] and command not in candidates:
                candidates.append(list(command))
                sources[tuple(command)] = label

        if self._fixed_python_command and self.python_command:
            add(list(self.python_command), "configured Python")
            self._candidate_sources = sources
            return candidates
        if self._manual_python and self.python_command:
            add(list(self.python_command), "manual selection")
        if not getattr(sys, "frozen", False) and sys.version_info[:2] in {(3, 11), (3, 12)}:
            add([sys.executable], "current Python")
        if sys.platform == "win32":
            # Some py-launcher installations select no interpreter for the bare
            # version selector but work with the explicit 64-bit selector.
            for version in ("3.11", "3.12"):
                add(["py", f"-{version}-64"], f"py -{version}-64")
            for command in self._py_launcher_candidates():
                add(command, "py launcher catalog")
            for version in ("3.11", "3.12"):
                add(["py", f"-{version}"], f"py -{version}")
            add(["python"], "python on PATH")
        else:
            for command in (["python3.11"], ["python3.12"], ["python3"], ["python"]):
                add(command, "python on PATH")
        self._candidate_sources = sources
        return candidates

    def _python_probe(self, command: list[str]) -> dict | None:
        code = (
            "import json,platform,sys\n"
            "try:\n"
            " import pip\n"
            " pip_version=pip.__version__\n"
            "except Exception:\n"
            " pip_version=None\n"
            "print(json.dumps({"
            "'version':list(sys.version_info[:3]),'machine':platform.machine(),"
            "'implementation':platform.python_implementation(),"
            "'bits':64 if sys.maxsize>2**32 else 32,"
            "'system':platform.system(),'executable':sys.executable,'pip':pip_version}))"
        )
        try:
            result = subprocess.run(
                [*command, "-I", "-c", code], capture_output=True, text=True,
                errors="replace", timeout=20, env=_environment(), check=False, shell=False,
            )
        except subprocess.TimeoutExpired:
            self._last_probe_error = "Python/pip probe timed out"
            return None
        except (OSError, ValueError, subprocess.SubprocessError):
            self._last_probe_error = "Python/pip probe could not be started"
            return None
        if result.returncode:
            self._last_probe_error = f"Python/pip probe exited with status {result.returncode}"
            return None
        self._last_probe_error = "probe returned invalid Python identity data"
        try:
            info = json.loads(result.stdout.strip())
            version = tuple(info["version"])
            if version[:2] not in {(3, 11), (3, 12)}:
                self._last_probe_error = "Python version is not 3.11 or 3.12"
                return None
            if info["machine"].lower() not in {"amd64", "x86_64"}:
                self._last_probe_error = "Python machine architecture is not AMD64/x86_64"
                return None
            if info.get("implementation") != "CPython" or info.get("bits") != 64:
                self._last_probe_error = "interpreter is not 64-bit CPython"
                return None
            if not isinstance(info.get("pip"), str) or not info["pip"]:
                self._last_probe_error = "pip is unavailable or did not report its version"
                return None
            if not isinstance(info.get("executable"), str) or not info["executable"]:
                self._last_probe_error = "Python did not report its executable identity"
                return None
            info["command"] = command
            self._last_probe_error = ""
            return info
        except (ValueError, TypeError, KeyError, AttributeError):
            return None

    def _python_requirement_error(self) -> str:
        message = "CPython 3.11/3.12 x64 with pip is required for the optional runtime"
        if not self._python_diagnostics:
            return message
        details = "; ".join(self._python_diagnostics[:8])
        omitted = len(self._python_diagnostics) - 8
        if omitted > 0:
            details += f"; {omitted} additional probe failures omitted"
        return message + ". Discovery: " + details

    def python(self, *, refresh: bool = False) -> dict | None:
        with self._lock:
            if self._python is not None and not refresh:
                return dict(self._python)
            self._python = None
            self._python_diagnostics = []
            for command in self._candidates():
                label = self._candidate_sources.get(tuple(command), "Python executable")
                info = self._python_probe(command)
                if info is None:
                    self._python_diagnostics.append(f"{label}: {self._last_probe_error}")
                    continue
                info["selection"] = (
                    "manual" if self._manual_python and command == self.python_command
                    else "automatic"
                )
                info["command_label"] = label
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
    def _validate_probe(result: dict, lock: dict, python: dict) -> None:
        expected = {
            item["normalized_name"]: item["version"]
            for item in applicable_packages(lock, python=python)
        }
        versions = result.get("package_versions") or {}
        normalized = {key.lower().replace("_", "-"): str(value) for key, value in versions.items()}

        def safe(value: object) -> str:
            if value is None:
                return "unknown"
            text = str(value)
            return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,63}", text) \
                else "<nonconforming>"

        if result.get("status") == "UNAVAILABLE" \
                and result.get("error_type") == "RUNTIME_IMPORT_ERROR":
            module = result.get("module")
            if module not in {"numpy", "pandas", "einops", "safetensors",
                              "huggingface_hub", "tqdm", "torch"}:
                module = "unknown"
            detail = safe(result.get("exception_type"))
            missing_name = safe(result.get("exception_name"))
            if missing_name != "unknown" and missing_name != module:
                detail += ":" + missing_name
            raise ArtifactError(f"Kronos runtime import failed for {module} ({detail})")
        if result.get("status") == "ERROR":
            detail = safe(result.get("error_type"))
            missing_name = safe(result.get("error_name"))
            if missing_name != "unknown":
                detail += ":" + missing_name
            stage = result.get("stage")
            if stage not in {"RUNTIME_PROBE", "OPERATOR_PAYLOAD", "REAL_INFERENCE"}:
                stage = "UNKNOWN"
            raise ArtifactError(f"Kronos runtime worker failed during {stage} ({detail})")
        if normalized != expected:
            missing = sorted(set(expected) - set(normalized))
            unexpected = sorted(set(normalized) - set(expected))
            mismatched = sorted(
                name for name in set(expected) & set(normalized)
                if expected[name] != normalized[name]
            )
            details = []
            if missing:
                details.append("missing=" + ",".join(missing[:8]))
            if unexpected:
                details.append("unexpected=" + ",".join(safe(name) for name in unexpected[:8]))
            if mismatched:
                details.append("versions=" + ",".join(
                    f"{name}:expected={expected[name]},actual={safe(normalized[name])}"
                    for name in mismatched[:8]
                ))
            omitted = sum(max(0, len(values) - 8) for values in (missing, unexpected, mismatched))
            if omitted:
                details.append(f"omitted={omitted}")
            raise ArtifactError(
                "Installed Kronos runtime does not exactly match the lock ("
                + "; ".join(details) + ")"
            )
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
            base["issues"] = [self._python_requirement_error()]
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
            self._validate_probe(result, lock, python)
            return {**base, **result, "status": "READY", "verified": True,
                    "python": python, "issues": [], "lock_sha256": lock["sha256"]}
        except (ArtifactError, OSError) as exc:
            return {**base, "status": "UNAVAILABLE", "issues": [str(exc)],
                    "lock_sha256": lock["sha256"]}

    def install(self, *, cancel: threading.Event,
                progress: Callable[[dict], None]) -> dict:
        python = self.python(refresh=True)
        if python is None:
            raise ArtifactError(self._python_requirement_error())
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
            self._validate_probe(result, lock, python)
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
