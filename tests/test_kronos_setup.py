"""Unit/security tests for Kronos setup; no external market/model access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import threading
import time

import pytest

from app.autonomy.kronos import KronosService
from app.autonomy.kronos_artifacts import (
    ArtifactError, ArtifactStore, DiskSpaceError, DownloadCancelled,
    KronosArtifactInstaller, SecureHuggingFaceDownloader, load_manifest,
    manifest_digest, sha256_file,
)
from app.autonomy.kronos_runtime import applicable_packages, runtime_lock
from app.autonomy.kronos_setup import KronosSetupManager
from app.config.settings import ROOT


def small_manifest(tmp_path: Path) -> tuple[Path, Path, dict[bytes, bytes]]:
    vendor = tmp_path / "vendor"
    source = b"# reviewed tiny test source\n"
    (vendor / "model").mkdir(parents=True)
    (vendor / "model" / "__init__.py").write_bytes(source)
    payloads = {
        b'{"model":true}\n': b"model weights fixture",
        b'{"tokenizer":true}\n': b"tokenizer weights fixture",
    }
    model_config, tokenizer_config = list(payloads)
    manifest = {
        "schema": "axiom.kronos.artifacts.v1",
        "source": {"repository": "https://github.com/shiyu-coder/Kronos", "commit": "a" * 40,
                   "files": {"model/__init__.py": hashlib.sha256(source).hexdigest()}},
        "model": {"repository": "NeoQuasar/Kronos-small", "revision": "b" * 40, "directory": "model",
                  "context": 8, "files": {}},
        "tokenizer": {"repository": "NeoQuasar/Kronos-Tokenizer-base", "revision": "c" * 40,
                      "directory": "tokenizer", "files": {}},
    }
    for component, contents in (("model", payloads), ("tokenizer", payloads)):
        manifest[component]["files"] = {
            "config.json" if index == 0 else "model.safetensors": {
                "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            } for index, content in enumerate(contents)
        }
    # Give each component distinct configuration bytes as well as weights.
    manifest["tokenizer"]["files"] = {
        "config.json": {"size": len(tokenizer_config),
                        "sha256": hashlib.sha256(tokenizer_config).hexdigest()},
        "model.safetensors": {"size": len(payloads[model_config]),
                              "sha256": hashlib.sha256(payloads[model_config]).hexdigest()},
    }
    manifest["model"]["files"] = {
        "config.json": {"size": len(model_config), "sha256": hashlib.sha256(model_config).hexdigest()},
        "model.safetensors": {"size": len(payloads[tokenizer_config]),
                              "sha256": hashlib.sha256(payloads[tokenizer_config]).hexdigest()},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    expected_content = {}
    for spec in (manifest["model"], manifest["tokenizer"]):
        for expected in spec["files"].values():
            for content in (source, *payloads.keys(), *payloads.values()):
                if hashlib.sha256(content).hexdigest() == expected["sha256"]:
                    expected_content[expected["sha256"]] = content
    return path, vendor, expected_content


class FixtureResponse:
    def __init__(self, content: bytes, cancel: threading.Event, cancel_after_read: bool):
        self.content = content
        self.cancel = cancel
        self.cancel_after_read = cancel_after_read
        self.offset = 0

    def getheader(self, name):
        return str(len(self.content)) if name.lower() == "content-length" else None

    def read(self, size):
        if self.offset >= len(self.content):
            return b""
        chunk = self.content[self.offset:self.offset + min(size, 1)]
        self.offset += len(chunk)
        if self.cancel_after_read:
            self.cancel.set()
        return chunk

    def close(self):
        pass


class FixtureDownloader:
    def __init__(self, contents: dict[bytes, bytes], *, corrupt: bool = False,
                 cancel_after_read: bool = False):
        self.contents = contents
        self.corrupt = corrupt
        self.cancel_after_read = cancel_after_read
        self.calls = []

    @staticmethod
    def url_for(repository, revision, filename):
        return SecureHuggingFaceDownloader.url_for(repository, revision, filename)

    def open(self, url, cancel):
        self.calls.append(url)
        # Fixture insertion order is the manifest's model/tokenizer file order.
        sequence = list(self.contents.values())
        content = sequence[(len(self.calls) - 1) % len(sequence)]
        if self.corrupt:
            content = b"X" + content[1:]
        return FixtureResponse(content, cancel, self.cancel_after_read), "huggingface.co"

    def close(self):
        pass


def test_atomic_install_and_offline_verification(tmp_path):
    manifest_path, vendor, contents = small_manifest(tmp_path)
    root = tmp_path / "per-user" / "kronos"
    store = ArtifactStore(root)
    installer = KronosArtifactInstaller(manifest_path, store, FixtureDownloader(contents))
    result = installer.install(cancel=threading.Event(), progress=lambda _value: None)
    manifest = load_manifest(manifest_path)
    assert result["manifest_sha256"] == manifest_digest(load_manifest(manifest_path))
    assert (root / "current.json").is_file()
    assert not store.partial_directories()
    active, layout = store.active_root(manifest_digest(manifest))
    assert layout == "ATOMIC_GENERATION"
    assert active.is_dir()
    generation_identity = active.stat().st_ino
    installer.install(cancel=threading.Event(), progress=lambda _value: None)
    assert active.stat().st_ino == generation_identity  # valid retry preserves active files
    assert not list(root.glob(".replaced-*"))
    # Cancellation must not publish a partial generation.
    other_root = tmp_path / "cancelled"
    other_store = ArtifactStore(other_root)
    with pytest.raises(DownloadCancelled):
        KronosArtifactInstaller(
            manifest_path, other_store,
            FixtureDownloader(contents, cancel_after_read=True),
        ).install(cancel=threading.Event(), progress=lambda _value: None)
    assert not (other_root / "current.json").exists()
    assert not other_store.partial_directories()
    # Legacy layouts remain verifiable, without auto-deleting unknown files.
    legacy = tmp_path / "legacy"
    shutil.copytree(active, legacy)
    inspection = ArtifactStore(legacy).inspect(manifest, vendor)
    assert inspection["artifact_verified"] is True


def test_hash_mismatch_is_never_published(tmp_path):
    manifest_path, vendor, contents = small_manifest(tmp_path)
    root = tmp_path / "artifacts"
    with pytest.raises(ArtifactError, match="SHA-256 mismatch"):
        KronosArtifactInstaller(
            manifest_path, ArtifactStore(root), FixtureDownloader(contents, corrupt=True),
        ).install(cancel=threading.Event(), progress=lambda _value: None)
    assert not (root / "current.json").exists()
    assert not ArtifactStore(root).partial_directories()


def test_disk_space_preflight(tmp_path, monkeypatch):
    manifest, _vendor, contents = small_manifest(tmp_path)
    usage = type("Usage", (), {"free": 100})()
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage)
    with pytest.raises(DiskSpaceError, match="Insufficient disk space"):
        KronosArtifactInstaller(
            manifest, ArtifactStore(tmp_path / "artifacts"), FixtureDownloader(contents),
        ).install(cancel=threading.Event(), progress=lambda _value: None)


class FakeHTTPResponse:
    def __init__(self, status, *, location="", content=b""):
        self.status = status
        self.location = location
        self.content = content
        self.offset = 0

    def getheader(self, name):
        if name.lower() == "location":
            return self.location or None
        if name.lower() == "content-length" and self.status == 200:
            return str(len(self.content))
        return None

    def read(self, size):
        chunk = self.content[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self):
        pass


class FakeConnection:
    responses = []
    requests = []

    def __init__(self, host, *args, **kwargs):
        self.host = host

    def request(self, method, path, headers):
        self.requests.append((self.host, method, path))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


def test_https_redirect_allowlist_and_integrity(monkeypatch):
    content = b"pinned exact response"
    url = SecureHuggingFaceDownloader.url_for(
        "NeoQuasar/Kronos-small", "a" * 40, "config.json",
    )
    monkeypatch.setattr("app.autonomy.kronos_artifacts.http.client.HTTPSConnection", FakeConnection)
    FakeConnection.responses = [
        FakeHTTPResponse(302, location="https://cas-server.xethub.hf.co/object"),
        FakeHTTPResponse(200, content=content),
    ]
    FakeConnection.requests = []
    downloader = SecureHuggingFaceDownloader()
    response, host = downloader.open(url, threading.Event())
    assert response.read(1000) == content
    assert host == "cas-server.xethub.hf.co"
    response.close()
    downloader.close()
    assert len(FakeConnection.requests) == 2
    for location in ("https://evil.example/object", "http://huggingface.co/object"):
        FakeConnection.responses = [FakeHTTPResponse(302, location=location)]
        with pytest.raises(ArtifactError, match="allowlist"):
            SecureHuggingFaceDownloader().open(url, threading.Event())
    with pytest.raises(ArtifactError, match="allowlist"):
        SecureHuggingFaceDownloader().open("https://evil.example/object", threading.Event())


def test_exact_runtime_lock_and_platform_markers(monkeypatch):
    import app.autonomy.kronos_runtime as runtime_module
    lock = runtime_lock(ROOT / "config" / "kronos-runtime-requirements.txt")
    assert len(lock["sha256"]) == 64
    monkeypatch.setattr(runtime_module.sys, "version_info", (3, 11, 0))
    monkeypatch.setattr(runtime_module.sys, "platform", "linux")
    linux = {item["name"]: item["version"] for item in applicable_packages(lock)}
    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    windows = {item["name"]: item["version"] for item in applicable_packages(lock)}
    assert linux["torch"] == "2.7.1+cpu"
    assert "nvidia-cublas-cu12" not in linux
    assert windows["colorama"] == "0.4.6"
    assert "colorama" not in linux
    assert "setuptools" not in windows
    monkeypatch.setattr(runtime_module.sys, "version_info", (3, 12, 0))
    python312 = {item["name"] for item in applicable_packages(lock)}
    assert "setuptools" in python312


def test_pip_commands_are_isolated_exact_and_index_scoped(tmp_path, monkeypatch):
    import app.autonomy.kronos_runtime as runtime_module
    manager = runtime_module.KronosRuntimeManager(
        tmp_path / "kronos", ROOT / "config" / "kronos-runtime-requirements.txt",
        tmp_path / "worker.py", application_root=ROOT,
    )
    python = {"version": [3, 11, 2], "system": "Linux", "machine": "x86_64",
              "executable": "/usr/bin/python3.11", "pip": "25.0"}
    monkeypatch.setattr(manager, "python", lambda refresh=False: python)
    commands = []
    monkeypatch.setattr(manager, "_run", lambda command, **kwargs: (commands.append(command) or (0, "", "")))
    lock = runtime_lock(manager.lock_path)
    versions = {item["normalized_name"]: item["version"] for item in applicable_packages(lock)}
    monkeypatch.setattr(manager, "_worker", lambda *args, **kwargs: {
        "package_versions": versions, "device": "cpu", "cuda_available": False,
        "torch_cuda_build": None, "status": "READY",
    })
    monkeypatch.setattr(manager, "probe", lambda cancel=None: {"status": "READY"})
    assert manager.install(cancel=threading.Event(), progress=lambda _value: None)["status"] == "READY"
    assert len(commands) == 2
    pypi, cpu = commands
    for command in commands:
        assert "--no-deps" in command and "--only-binary=:all:" in command
        assert "--isolated" in command and "--no-cache-dir" in command
    assert "https://pypi.org/simple" in pypi
    assert not any(item.startswith("torch==") for item in pypi)
    assert "https://download.pytorch.org/whl/cpu" in cpu
    assert "torch==2.7.1+cpu" in cpu
    assert not any(item.startswith("numpy==") for item in cpu)


def test_isolated_worker_probe_only_contract(tmp_path):
    """Fake import stubs test process mechanics, never model inference."""
    import app.autonomy.kronos_runtime as runtime_module
    manager = runtime_module.KronosRuntimeManager(
        tmp_path / "kronos", ROOT / "config" / "kronos-runtime-requirements.txt",
        ROOT / "app/autonomy/kronos_worker.py", application_root=ROOT,
        python_command=[sys.executable],
    )
    python = manager.python()
    assert python is not None
    directory = manager.runtime_dir(python)
    site = directory / "site"
    site.mkdir(parents=True)
    lock = runtime_lock(manager.lock_path)
    expected = {}
    for item in applicable_packages(lock):
        expected[item["normalized_name"]] = item["version"]
        dist = site / f"{item['normalized_name']}-{item['version']}.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {item['name']}\nVersion: {item['version']}\n",
            encoding="utf-8",
        )
    for name in ("numpy", "pandas", "einops", "safetensors", "huggingface_hub", "tqdm"):
        (site / f"{name}.py").write_text(f"__version__ = {expected[name.replace('_', '-')]!r}\n", encoding="utf-8")
    torch = site / "torch"
    torch.mkdir()
    (torch / "__init__.py").write_text(
        "__version__ = '2.7.1+cpu'\n"
        "class Cuda:\n    @staticmethod\n    def is_available(): return False\n"
        "class Version:\n    cuda = None\n"
        "cuda = Cuda()\nversion = Version()\n"
        "def set_num_threads(value): pass\n"
        "def set_num_interop_threads(value): pass\n", encoding="utf-8",
    )
    (directory / "axiom-runtime.json").write_text(json.dumps({
        "schema": "axiom.kronos.runtime.v1", "lock_sha256": lock["sha256"],
        "python_version": python["version"], "device": "cpu", "package_versions": expected,
    }), encoding="utf-8")
    result = manager.probe()
    assert result["status"] == "READY", result
    assert result["package_versions"] == expected
    assert result["device"] == "cpu" and result["cuda_available"] is False
    assert result["installed"] is True and result["verified"] is True


class FakeRuntime:
    def __init__(self, *, ready=True, worker_error=False):
        self.ready = ready
        self.worker_error = worker_error

    def probe(self):
        return {"status": "READY" if self.ready else "UNAVAILABLE", "verified": self.ready,
                "issues": [] if self.ready else ["MISSING: exact runtime"],
                "runtime_lock_sha256": "f" * 64, "package_versions": {"torch": "2.7.1+cpu"},
                "direct_versions": {"torch": "2.7.1+cpu"}, "cuda_available": False}

    def install_plan(self):
        return {"lock_sha256": "f" * 64, "packages": [], "python": "3.11/3.12",
                "indexes": [], "minimum_free_bytes": 0}

    def forecast(self, payload):
        if self.worker_error:
            return {"status": "ERROR", "error_type": "RUNTIME_IMPORT_ERROR"}
        last = payload["bars"][-1]["close"]
        return {"status": "FORECAST", "real_forecast": True, "mock_outputs_used": False,
                "device": "cpu", "manifest_sha256": payload["manifest_sha256"],
                "model_revision": "b" * 40, "tokenizer_revision": "c" * 40,
                "load_ms": 1.0, "inference_ms": 2.0, "runtime": self.probe(),
                "forecast": {"mean_close": last + 1, "q10_close": last,
                             "q90_close": last + 2, "up_probability": 1.0}}

    def cancel_active(self):
        pass


def candles():
    from datetime import datetime, timedelta, timezone
    from app.data.models import Candle
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Candle(start + timedelta(minutes=i), 100, 102, 99, 101, 1000, "TEST")
            for i in range(20)]


def test_service_requires_runtime_and_has_no_fallback(tmp_path):
    manifest, vendor, contents = small_manifest(tmp_path)
    KronosArtifactInstaller(manifest, ArtifactStore(tmp_path / "off"), FixtureDownloader(contents)).install(cancel=threading.Event(), progress=lambda _value: None)
    service = KronosService(manifest, tmp_path / "off", vendor,
                            runtime_manager=FakeRuntime(ready=False),
                            runtime_lock_path=tmp_path / "unused-lock", worker_path=tmp_path / "unused-worker")
    assert service.verify()["available_for_inference"] is False
    with pytest.raises(ArtifactError, match="UNAVAILABLE"):
        service.forecast("TEST", candles(), timeframe="1m")
    service.runtime.ready = True
    assert service.verify()["available_for_inference"] is True
    forecast = service.forecast("TEST", candles(), timeframe="1m")
    assert forecast.artifact_digest == service.artifact_digest
    assert service.status()["last_inference"]["status"] == "REAL_FORECAST"
    record = service.self_test()
    assert record["status"] == "PASSED" and record["device"] == "cpu"
    assert record["manifest_sha256"] == service.artifact_digest
    assert record["manifest_file_sha256"] == sha256_file(manifest)
    assert record["runtime_lock_sha256"] == "f" * 64
    assert record["input_label"].startswith("SYNTHETIC_INFERENCE_SMOKE_ONLY")
    assert record["market_evaluation"] is False and record["profitability_evidence"] is False
    assert record["inference_ms"] > 0 and record["load_ms"] > 0
    assert set(record["artifacts"]) == {"model", "tokenizer"}
    assert json.loads((tmp_path / "off" / "self-test.json").read_text())["status"] == "PASSED"
    service.runtime.worker_error = True
    with pytest.raises(ArtifactError, match="RUNTIME_IMPORT_ERROR"):
        service.forecast("TEST", candles(), timeframe="1m")
    assert service.status()["status"] == "INFERENCE_ERROR"
    assert service.status()["available_for_inference"] is False


class SetupService:
    def __init__(self):
        self.installer = self
        self.runtime = self
        self.cancel_seen = threading.Event()

    def install(self, *, cancel, progress):
        progress({"phase": "VERIFYING", "manifest_sha256": "a" * 64})
        if cancel.is_set():
            raise DownloadCancelled("cancelled")

    def verify(self):
        return {"available_for_inference": True}

    def self_test(self):
        return {"status": "PASSED", "manifest_sha256": "a" * 64,
                "runtime_lock_sha256": "f" * 64, "inference_ms": 2.0}

    def status(self):
        return {"status": "READY", "available_for_inference": True}

    def install_plan(self):
        return {"manifest_sha256": "a" * 64}

    def close(self):
        self.cancel_seen.set()


def test_setup_manager_global_audit_and_no_execution_action():
    service = SetupService()
    events = []
    manager = KronosSetupManager(service, audit=lambda event, payload: events.append((event, payload)))
    view = manager.start_install()
    deadline = time.monotonic() + 2
    while view["operation"]["active"] and time.monotonic() < deadline:
        time.sleep(0.01)
        view = manager.status()
    assert view["operation"]["state"] == "COMPLETED"
    assert view["execution_effect"].startswith("NONE")
    assert events[0][0] == "KRONOS_SETUP_STARTED"
    assert any(event == "KRONOS_SETUP_VERIFIED" for event, _payload in events)
    assert all("session_id" not in payload for _event, payload in events)
    manager.close()
    assert service.cancel_seen.is_set()


def test_setup_cancel_reverifies_actual_state():
    class BlockingService(SetupService):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.verify_calls = 0

        def install(self, *, cancel, progress):
            self.started.set()
            if cancel.wait(2):
                raise DownloadCancelled("cancelled by test")
            raise AssertionError("cancel was not delivered")

        def verify(self):
            self.verify_calls += 1
            return {"status": "UNAVAILABLE", "available_for_inference": False}

    service = BlockingService()
    manager = KronosSetupManager(service)
    manager.start_install()
    assert service.started.wait(1)
    manager.cancel()
    deadline = time.monotonic() + 3
    view = manager.status()
    while view["operation"]["active"] and time.monotonic() < deadline:
        time.sleep(0.01)
        view = manager.status()
    assert view["operation"]["state"] == "CANCELLED"
    assert view["operation"]["error"]["code"] == "CANCELLED"
    assert service.verify_calls >= 1
    assert service.cancel_seen.is_set()
