"""Background operator actions for pinned Kronos setup and self-test."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Callable
from uuid import uuid4

from app.autonomy.kronos_artifacts import ArtifactError, DiskSpaceError, DownloadCancelled


class KronosSetupManager:
    def __init__(self, service, *, audit: Callable | None = None,
                 on_status: Callable | None = None):
        self.service = service
        self.audit = audit or (lambda *_args, **_kwargs: None)
        self.on_status = on_status or (lambda _status: None)
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._operation = {
            "operation_id": None, "kind": None, "state": "IDLE",
            "cancel_requested": False, "phase": "IDLE", "error": None,
        }

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _update(self, **values) -> None:
        with self._lock:
            self._operation.update(values)

    def _progress(self, values: dict) -> None:
        allowed = {
            "phase", "component", "file", "bytes", "total_bytes", "file_bytes",
            "file_total_bytes", "manifest_sha256", "runtime_lock_sha256",
        }
        safe = {key: value for key, value in values.items() if key in allowed}
        with self._lock:
            previous = self._operation.get("phase")
        self._update(**safe)
        phase = safe.get("phase")
        if phase and phase != previous:
            self.audit("KRONOS_SETUP_PHASE", {
                "operation_id": self._operation["operation_id"], "phase": phase,
                "component": safe.get("component"), "file": safe.get("file"),
                "manifest_sha256": safe.get("manifest_sha256"),
                "runtime_lock_sha256": safe.get("runtime_lock_sha256"),
            })

    def _start(self, kind: str) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ArtifactError("Another Kronos setup/self-test operation is already running")
            self._cancel = threading.Event()
            operation_id = uuid4().hex
            self._operation = {
                "operation_id": operation_id, "kind": kind, "state": "RUNNING",
                "cancel_requested": False, "phase": "STARTING", "error": None,
                "started_at": self._now(), "completed_at": None,
                "bytes": 0, "total_bytes": 0,
            }
            self._thread = threading.Thread(
                target=self._run, args=(kind, operation_id),
                name=f"axiom-kronos-{kind}", daemon=True,
            )
            self._thread.start()
        return self.status()

    def start_install(self) -> dict:
        return self._start("INSTALL_PINNED")

    def start_self_test(self) -> dict:
        return self._start("OFFLINE_SELF_TEST")

    def select_python(self, executable: str | None) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ArtifactError("Python selection is locked during Kronos setup/self-test")
        refreshed = self.service.select_python(executable)
        self.on_status(refreshed)
        return self.status()

    def cancel(self) -> dict:
        self._cancel.set()
        self._update(cancel_requested=True)
        # Close only this setup's active connection/child process.  No trading
        # session, provider, broker connection or order is touched.
        self.service.close()
        self._update(phase="CANCELLING")
        return self.status()

    def _run(self, kind: str, operation_id: str) -> None:
        try:
            self.audit("KRONOS_SETUP_STARTED", {"operation_id": operation_id, "kind": kind})
            if kind == "INSTALL_PINNED":
                self.service.installer.install(cancel=self._cancel, progress=self._progress)
                if self._cancel.is_set():
                    raise DownloadCancelled("Kronos setup cancelled")
                self.service.runtime.install(cancel=self._cancel, progress=self._progress)
                if self._cancel.is_set():
                    raise DownloadCancelled("Kronos setup cancelled")
                verified = self.service.verify()
                if not verified["available_for_inference"]:
                    raise ArtifactError("Kronos setup remained UNAVAILABLE after verification")
                result = self.service.self_test()
                self.audit("KRONOS_SETUP_VERIFIED", {
                    "operation_id": operation_id,
                    "manifest_sha256": result["manifest_sha256"],
                    "runtime_lock_sha256": result.get("runtime_lock_sha256"),
                    "inference_ms": result.get("inference_ms"), "device": "cpu",
                    "real_forecast": True,
                })
            else:
                result = self.service.self_test()
                self.audit("KRONOS_SELF_TEST", {
                    "operation_id": operation_id, "status": result["status"],
                    "manifest_sha256": result["manifest_sha256"],
                    "inference_ms": result.get("inference_ms"), "device": "cpu",
                })
            if self._cancel.is_set():
                raise DownloadCancelled("Kronos operation cancelled")
            self._update(state="COMPLETED", phase="COMPLETED", completed_at=self._now(),
                         result={"status": result["status"],
                                 "manifest_sha256": result["manifest_sha256"],
                                 "inference_ms": result.get("inference_ms")})
        except DownloadCancelled:
            self._update(state="CANCELLED", phase="CANCELLED", completed_at=self._now(),
                         error={"code": "CANCELLED", "message": "Kronos operation cancelled; partial files were removed"})
            self.audit("KRONOS_SETUP_CANCELLED", {"operation_id": operation_id})
        except DiskSpaceError as exc:
            self._update(state="ERROR", phase="ERROR", completed_at=self._now(),
                         error={"code": "DISK_SPACE", "message": str(exc)})
            self.audit("KRONOS_SETUP_ERROR", {"operation_id": operation_id, "code": "DISK_SPACE"})
        except ArtifactError as exc:
            self._update(state="ERROR", phase="ERROR", completed_at=self._now(),
                         error={"code": "SETUP_UNAVAILABLE", "message": str(exc)})
            self.audit("KRONOS_SETUP_ERROR", {"operation_id": operation_id, "code": "SETUP_UNAVAILABLE"})
        except Exception as exc:
            self._update(state="ERROR", phase="ERROR", completed_at=self._now(),
                         error={"code": type(exc).__name__,
                                "message": "Kronos operation failed; no mock forecast or fallback installation was used"})
            self.audit("KRONOS_SETUP_ERROR", {"operation_id": operation_id, "code": type(exc).__name__})
        finally:
            with self._lock:
                completed = self._operation.get("state") == "COMPLETED"
            try:
                refreshed = self.service.status() if completed else self.service.verify()
            except Exception:
                refreshed = self.service.status()
            self.on_status(refreshed)

    def status(self) -> dict:
        with self._lock:
            operation = dict(self._operation)
            active = self._thread is not None and self._thread.is_alive()
        operation["active"] = active
        try:
            plan = self.service.install_plan()
            plan_error = ""
        except ArtifactError as exc:
            plan = {}
            plan_error = str(exc)
        return {
            **self.service.status(), "operation": operation, "install_plan": plan,
            "install_plan_error": plan_error,
            "execution_effect": "NONE; setup never creates/Arms/Activates a session or order",
            "network_scope": "Pinned public Hugging Face artifacts and locked Python packages only",
            "broker_credentials_used": False,
        }

    def close(self) -> None:
        self._cancel.set()
        self.service.close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
