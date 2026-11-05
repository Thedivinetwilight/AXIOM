"""Kronos Forecast Provider & PAPER Session Lifecycle Coordinator.

Wraps AXIOM's pinned ``KronosService`` (NeoQuasar/Kronos-small +
Kronos-Tokenizer-base, locked CPU runtime, MIT upstream source) with:
- Six Model-Lab provider states (``NOT_INSTALLED``, ``NOT_VERIFIED``, ``READY``,
  ``RUNNING``, ``ERROR``, ``STALE``) plus seven explicit session-bound PAPER
  lifecycle states (``NOT_CONFIGURED``, ``READY``, ``LOADING``, ``RUNNING``,
  ``DEGRADED``, ``FAILED``, ``STOPPED``).
- Strict artifact SHA-256 + locked CPU runtime + exact interpreter ``self-test.json``
  identity enforcement before loading or running inference.
- Bounded session inference with explicit timeouts, input validation, output
  validation, measured latency, and clean worker shutdown when PAPER stops.
- Explicit distinction between installed, validated, loaded, running, forecast
  produced, and forecast actually consumed by an active PAPER strategy.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.autonomy.kronos import KronosService
from app.autonomy.kronos_artifacts import ArtifactError
from app.data.models import TIMEFRAMES, Candle, parse_timestamp
from app.monitoring.paper_events import normalize_structured_event, redact_sensitive_data

KRONOS_PROVIDER_STATES = (
    "NOT_INSTALLED",
    "NOT_VERIFIED",
    "READY",
    "RUNNING",
    "ERROR",
    "STALE",
)
KRONOS_LIFECYCLE_STATES = (
    "NOT_CONFIGURED",
    "READY",
    "LOADING",
    "RUNNING",
    "DEGRADED",
    "FAILED",
    "STOPPED",
)
KRONOS_FAILURE_POLICIES = (
    "FAIL_CLOSED_NO_ENTRY",
    "EXPLICIT_NON_KRONOS_FALLBACK",
)
KRONOS_CONFLICT_POLICIES = (
    "VETO_CONFLICTING_ENTRY",
    "REDUCE_SCORE_ON_CONFLICT",
)
SUPPORTED_MARKETS = frozenset({"NSE", "NASDAQ"})
MIN_CONTEXT_BARS = 32
MAX_CONTEXT_BARS = 512


def _utc_now(clock: Callable[[], datetime] | None = None) -> datetime:
    now = clock() if clock is not None else datetime.now(UTC)
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 3)
    idx = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return round(sorted_vals[lo], 3)
    weight = idx - lo
    return round(sorted_vals[lo] * (1.0 - weight) + sorted_vals[hi] * weight, 3)


def _process_rss_mb() -> float | None:
    """Best-effort current process RSS in MiB (POSIX /proc or Windows ctypes)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            proc = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(proc, ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / (1024.0 * 1024.0), 2)
        except (OSError, AttributeError, ValueError, TypeError) as exc:
            _ = exc
            return None
    status_file = Path("/proc/self/status")
    if status_file.is_file():
        try:
            for line in status_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return round(kb / 1024.0, 2)
        except (OSError, ValueError, IndexError):
            return None
    return None


@dataclass
class KronosLabConfig:
    inference_enabled: bool = False
    model_loaded_preference: bool = False
    cadence_seconds: int = 300
    timeout_seconds: float = 45.0
    horizon: int = 5
    samples: int = 8
    min_context_bars: int = 32
    max_context_bars: int = 256
    max_forecast_age_seconds: int = 900
    max_bar_staleness_seconds: int = 86_400 * 7
    cpu_threads_cap: int = 4
    # Explicit, visible, auditable PAPER strategy contribution & failure policy
    paper_session_enabled: bool = False
    weight_cap: float = 0.20
    min_confidence: float = 0.55
    max_uncertainty_pct: float = 8.0
    conflict_policy: str = "VETO_CONFLICTING_ENTRY"
    failure_policy: str = "FAIL_CLOSED_NO_ENTRY"

    def validate(self) -> None:
        if not (15 <= self.cadence_seconds <= 86_400):
            raise ValueError("cadence_seconds must be between 15 and 86400")
        if not (1.0 <= self.timeout_seconds <= 300.0):
            raise ValueError("timeout_seconds must be between 1 and 300 seconds")
        if not (1 <= self.horizon <= 60):
            raise ValueError("horizon must be between 1 and 60 bars")
        if not (4 <= self.samples <= 32):
            raise ValueError("samples must be between 4 and 32")
        if not (MIN_CONTEXT_BARS <= self.min_context_bars <= self.max_context_bars <= MAX_CONTEXT_BARS):
            raise ValueError(f"context bounds must satisfy {MIN_CONTEXT_BARS} <= min <= max <= {MAX_CONTEXT_BARS}")
        if not (30 <= self.max_forecast_age_seconds <= 86_400):
            raise ValueError("max_forecast_age_seconds must be between 30 and 86400")
        if not (1 <= self.cpu_threads_cap <= 16):
            raise ValueError("cpu_threads_cap must be between 1 and 16")
        if not (0.0 <= float(self.weight_cap) <= 0.25):
            raise ValueError("weight_cap must be between 0.0 and 0.25 (advisory cap)")
        if not (0.50 <= float(self.min_confidence) <= 0.95):
            raise ValueError("min_confidence must be between 0.50 and 0.95")
        if not (0.5 <= float(self.max_uncertainty_pct) <= 50.0):
            raise ValueError("max_uncertainty_pct must be between 0.5 and 50.0")
        if self.conflict_policy not in KRONOS_CONFLICT_POLICIES:
            raise ValueError(f"conflict_policy must be one of {KRONOS_CONFLICT_POLICIES}")
        if self.failure_policy not in KRONOS_FAILURE_POLICIES:
            raise ValueError(f"failure_policy must be one of {KRONOS_FAILURE_POLICIES}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KronosInputError(ValueError):
    """Invalid symbol, market, timeframe, ordering, NaN, or context bars."""


class KronosUnavailableError(RuntimeError):
    """Fail-closed error when Kronos cannot produce a verified real forecast."""


class KronosOutputValidationError(KronosUnavailableError):
    """Rejected malformed, non-finite, out-of-bounds, or mismatched Kronos output."""


def _self_test_passed(base_view: dict[str, Any]) -> bool:
    if base_view.get("self_test_verified") is True:
        return True
    self_test = base_view.get("last_self_test")
    return isinstance(self_test, dict) and self_test.get("status") == "PASSED"


class KronosForecastProvider:
    """Well-defined, fail-closed Kronos inference & PAPER session lifecycle manager."""

    execution_authorization_effect = "NONE"
    mock_fallback_permitted = False

    def __init__(
        self,
        kronos_service: KronosService | None,
        *,
        setup_manager: Any | None = None,
        config_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.kronos = kronos_service
        self.setup_manager = setup_manager
        self._config_path = Path(config_path) if config_path is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        self.config = KronosLabConfig()
        self._load_persisted_config()
        # Safety rule: restart NEVER resumes hidden inference jobs or auto-loads weights.
        self.config.model_loaded_preference = False
        self._active_inferences = 0
        self._last_error: dict[str, Any] | None = None
        self._forecasts: dict[str, dict[str, Any]] = {}
        self._load_ms_samples: deque[float] = deque(maxlen=128)
        self._infer_ms_samples: deque[float] = deque(maxlen=128)

        # Session-tied PAPER lifecycle tracking (disarmed on startup/recovery)
        self._session_lifecycle_state: str = "NOT_CONFIGURED"
        self._session_lifecycle_reason: str = "No PAPER session has started Kronos in this process."
        self._attached_sessions: dict[str, dict[str, Any]] = {}
        self._session_worker_loaded: bool = False
        self._last_inference_at: str | None = None
        self._last_latency_ms: float | None = None
        self._forecast_produced_count: int = 0
        self._last_produced_at: str | None = None
        self._last_produced_summary: dict[str, Any] | None = None
        self._forecast_consumed_count: int = 0
        self._last_consumed_at: str | None = None
        self._last_consumed_summary: dict[str, Any] | None = None
        self._timeout_count: int = 0
        self._invalid_output_count: int = 0
        self._lifecycle_events: deque[dict[str, Any]] = deque(maxlen=400)
        self._event_sinks: list[Callable[[dict[str, Any]], None]] = []

    def register_event_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._event_sinks.append(sink)

    def _emit_lifecycle_event(
        self,
        event_type: str,
        *,
        market: str = "SYSTEM",
        symbol: str = "",
        strategy: str = "",
        session_id: str = "",
        decision_id: str = "",
        severity: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now_iso = _utc_now(self._clock).isoformat()
        raw_payload: dict[str, Any] = {
            "simulated": True,
            "real_money": False,
            "market": market,
            "symbol": symbol,
            "strategy": strategy,
            "session_run_id": session_id,
            "decision_id": decision_id,
            **(detail or {}),
        }
        if severity:
            raw_payload["severity"] = severity
        norm = normalize_structured_event(
            event_id=f"krn-{uuid4().hex[:12]}",
            timestamp=now_iso,
            event_type=event_type,
            market=market,
            source="KRONOS_PROVIDER",
            payload=raw_payload,
        )
        sinks: list[Callable[[dict[str, Any]], None]] = []
        with self._lock:
            self._lifecycle_events.append(norm)
            sinks = list(self._event_sinks)
        for sink in sinks:
            try:
                sink(norm)
            except Exception as exc:
                _ = exc
        return norm

    def lifecycle_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._lifecycle_events)
        items.reverse()
        return items[: max(1, min(int(limit), 1000))]

    def _load_persisted_config(self) -> None:
        if self._config_path is None or not self._config_path.is_file():
            return
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = KronosLabConfig(
                    inference_enabled=bool(raw.get("inference_enabled", False)),
                    model_loaded_preference=False,  # never auto-load on restart
                    cadence_seconds=int(raw.get("cadence_seconds", 300)),
                    timeout_seconds=float(raw.get("timeout_seconds", 45.0)),
                    horizon=int(raw.get("horizon", 5)),
                    samples=int(raw.get("samples", 8)),
                    min_context_bars=int(raw.get("min_context_bars", 32)),
                    max_context_bars=int(raw.get("max_context_bars", 256)),
                    max_forecast_age_seconds=int(raw.get("max_forecast_age_seconds", 900)),
                    max_bar_staleness_seconds=int(raw.get("max_bar_staleness_seconds", 86_400 * 7)),
                    cpu_threads_cap=int(raw.get("cpu_threads_cap", 4)),
                    paper_session_enabled=bool(raw.get("paper_session_enabled", False)),
                    weight_cap=float(raw.get("weight_cap", 0.20)),
                    min_confidence=float(raw.get("min_confidence", 0.55)),
                    max_uncertainty_pct=float(raw.get("max_uncertainty_pct", 8.0)),
                    conflict_policy=str(raw.get("conflict_policy", "VETO_CONFLICTING_ENTRY")),
                    failure_policy=str(raw.get("failure_policy", "FAIL_CLOSED_NO_ENTRY")),
                )
                cfg.validate()
                self.config = cfg
        except (OSError, ValueError, TypeError):
            self.config = KronosLabConfig()

    def _save_config(self) -> None:
        if self._config_path is None:
            return
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self.config)
        payload["auto_run_after_restart"] = False
        tmp = self._config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._config_path)

    def update_config(self, **updates: Any) -> dict[str, Any]:
        with self._lock:
            merged = asdict(self.config)
            for key, val in updates.items():
                if key in merged and val is not None:
                    merged[key] = val
            candidate = KronosLabConfig(**merged)
            candidate.validate()
            self.config = candidate
            self._save_config()
            return self.status()

    def paper_policy_view(self) -> dict[str, Any]:
        with self._lock:
            return {
                "paper_session_enabled": self.config.paper_session_enabled,
                "weight_cap": self.config.weight_cap,
                "min_confidence": self.config.min_confidence,
                "max_uncertainty_pct": self.config.max_uncertainty_pct,
                "conflict_policy": self.config.conflict_policy,
                "failure_policy": self.config.failure_policy,
                "supported_failure_policies": list(KRONOS_FAILURE_POLICIES),
                "supported_conflict_policies": list(KRONOS_CONFLICT_POLICIES),
                "kronos_can_independently_place_orders": False,
                "bypasses_paper_risk_gates": False,
                "live_authorization_effect": "NONE",
            }

    def set_inference_enabled(self, enabled: bool) -> dict[str, Any]:
        """Toggle Model-Lab forecasting only; never alters trading authorization."""
        with self._lock:
            self.config.inference_enabled = bool(enabled)
            self._save_config()
            return self.status()

    def load_model(self) -> dict[str, Any]:
        """Verify artifacts, runtime, AND exact self-test identity before marking loaded."""
        with self._lock:
            if self.kronos is None:
                self._last_error = {"code": "SERVICE_MISSING", "message": "KronosService is not initialized"}
                raise KronosUnavailableError("KronosService is not initialized")
            verified = self.kronos.verify()
            if not verified.get("available_for_inference"):
                issues = "; ".join(verified.get("issues") or ["Kronos artifacts or CPU runtime not installed"])
                raise KronosUnavailableError(f"Cannot load Kronos: {issues}")
            if not _self_test_passed(verified):
                raise KronosUnavailableError(
                    "Cannot load Kronos: offline CPU self-test has not PASSED for the exact configured runtime/artifact identity."
                )
            self.config.model_loaded_preference = True
            self._last_error = None
            return self.status()

    def unload_model(self) -> dict[str, Any]:
        """Terminate any active worker subprocess and mark Kronos unloaded."""
        with self._lock:
            self.config.model_loaded_preference = False
            self._session_worker_loaded = False
            if self.kronos is not None:
                stopper = getattr(self.kronos, "stop_session_worker", None)
                if callable(stopper):
                    stopper()
                self.kronos.close()
            if not self._attached_sessions:
                self._session_lifecycle_state = "STOPPED"
                self._session_lifecycle_reason = "Kronos worker explicitly unloaded by operator."
        self._emit_lifecycle_event(
            "KRONOS_STOPPED",
            detail={"lifecycle_state": "STOPPED", "reason": "operator unload_model"},
        )
        return self.status()

    def cancel_active(self) -> dict[str, Any]:
        """Cancel active setup/self-test or subprocess inference without touching trading."""
        with self._lock:
            if self.setup_manager is not None:
                self.setup_manager.cancel()
            elif self.kronos is not None:
                self.kronos.close()
            self._session_worker_loaded = False
            return self.status()

    # ------------------------------------------------------------------
    # Explicit PAPER Session Lifecycle Integration
    # ------------------------------------------------------------------

    def is_configured_for_paper_strategy(
        self,
        strategy: str,
        *,
        explicit_kronos_enabled: bool | None = None,
        ensemble_kronos_enabled: bool = False,
    ) -> bool:
        strat = (strategy or "").strip().lower()
        if strat == "manual_only":
            return bool(explicit_kronos_enabled is True)
        if explicit_kronos_enabled is not None:
            return bool(explicit_kronos_enabled)
        if strat in {"kronos_ensemble", "kronos_informed"}:
            return True
        if strat == "parallel_ensemble":
            return bool(ensemble_kronos_enabled or self.config.paper_session_enabled)
        return bool(self.config.paper_session_enabled)

    def start_for_paper_session(
        self,
        session_key: str,
        *,
        session_id: str,
        market: str,
        strategy: str,
        kronos_requested: bool,
        failure_policy: str | None = None,
    ) -> dict[str, Any]:
        """Check, validate exact identity, and start Kronos for an explicit PAPER session."""
        mkt = (market or "LOCAL").strip().upper()
        strat = (strategy or "manual_only").strip()
        eff_policy = str(failure_policy or self.config.failure_policy)
        if eff_policy not in KRONOS_FAILURE_POLICIES:
            raise ValueError(f"Unsupported Kronos failure policy {eff_policy!r}")

        now_iso = _utc_now(self._clock).isoformat()
        self._emit_lifecycle_event(
            "KRONOS_CHECK",
            market=mkt,
            strategy=strat,
            session_id=session_id,
            detail={
                "session_key": session_key,
                "kronos_requested": bool(kronos_requested),
                "failure_policy": eff_policy,
            },
        )

        if not kronos_requested:
            rec = {
                "session_key": session_key,
                "session_id": session_id,
                "market": mkt,
                "strategy": strat,
                "kronos_configured": False,
                "lifecycle_state": "NOT_CONFIGURED",
                "failure_policy": eff_policy,
                "reason": f"Strategy '{strat}' started without Kronos forecasting configured.",
                "started_at": now_iso,
                "updated_at": now_iso,
            }
            with self._lock:
                self._attached_sessions[session_key] = rec
                if not any(s.get("kronos_configured") for s in self._attached_sessions.values()):
                    self._session_lifecycle_state = "NOT_CONFIGURED"
                    self._session_lifecycle_reason = rec["reason"]
            self._emit_lifecycle_event(
                "KRONOS_NOT_CONFIGURED",
                market=mkt,
                strategy=strat,
                session_id=session_id,
                detail=rec,
            )
            return dict(rec)

        with self._lock:
            self._session_lifecycle_state = "LOADING"
            self._session_lifecycle_reason = f"Verifying & loading Kronos for {mkt} PAPER session {session_id}…"

        self._emit_lifecycle_event(
            "KRONOS_LOAD",
            market=mkt,
            strategy=strat,
            session_id=session_id,
            detail={"lifecycle_state": "LOADING", "session_key": session_key},
        )

        if self.kronos is None:
            reason = "KronosService is not initialized in this process."
            return self._record_session_start_failure(
                session_key=session_key,
                session_id=session_id,
                market=mkt,
                strategy=strat,
                failure_policy=eff_policy,
                code="SERVICE_MISSING",
                reason=reason,
            )

        t0 = time.perf_counter()
        try:
            verified = self.kronos.verify()
        except Exception as exc:
            return self._record_session_start_failure(
                session_key=session_key,
                session_id=session_id,
                market=mkt,
                strategy=strat,
                failure_policy=eff_policy,
                code="VERIFY_ERROR",
                reason=f"Kronos verification error ({type(exc).__name__})",
            )

        issues = [str(x) for x in (verified.get("issues") or [])]
        if any("MISMATCH" in x.upper() or "CORRUPT" in x.upper() for x in issues):
            return self._record_session_start_failure(
                session_key=session_key,
                session_id=session_id,
                market=mkt,
                strategy=strat,
                failure_policy=eff_policy,
                code="ARTIFACT_IDENTITY_MISMATCH",
                reason="; ".join(issues),
            )
        if not verified.get("available_for_inference") or verified.get("inference_runtime") != "READY":
            return self._record_session_start_failure(
                session_key=session_key,
                session_id=session_id,
                market=mkt,
                strategy=strat,
                failure_policy=eff_policy,
                code="ARTIFACTS_OR_RUNTIME_MISSING",
                reason="; ".join(issues) or "Kronos weights or locked CPU runtime are not installed.",
            )
        if not _self_test_passed(verified):
            return self._record_session_start_failure(
                session_key=session_key,
                session_id=session_id,
                market=mkt,
                strategy=strat,
                failure_policy=eff_policy,
                code="SELF_TEST_NOT_PASSED",
                reason="Offline CPU self-test has not PASSED for the exact configured manifest/lock/Python identity.",
            )

        # Start or confirm warm session worker
        load_ms = 0.0
        starter = getattr(self.kronos, "start_session_worker", None)
        try:
            if callable(starter):
                w_info = starter(
                    cpu_threads=self.config.cpu_threads_cap,
                    timeout=min(90.0, max(5.0, float(self.config.timeout_seconds))),
                )
                load_ms = float(w_info.get("load_ms") or ((time.perf_counter() - t0) * 1000.0))
            else:
                load_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        except Exception as exc:
            if self.kronos is not None:
                self.kronos.close()
            return self._record_session_start_failure(
                session_key=session_key,
                session_id=session_id,
                market=mkt,
                strategy=strat,
                failure_policy=eff_policy,
                code="WORKER_STARTUP_FAILED",
                reason=f"Kronos CPU worker failed to start ({type(exc).__name__}): {str(exc)[:180]}",
            )

        identity = {
            "manifest_sha256": str(verified.get("manifest_sha256") or verified.get("artifact_digest") or ""),
            "model_revision": str(verified.get("model_revision") or ""),
            "tokenizer_revision": str(verified.get("tokenizer_revision") or ""),
            "device": "cpu",
        }
        rec = {
            "session_key": session_key,
            "session_id": session_id,
            "market": mkt,
            "strategy": strat,
            "kronos_configured": True,
            "lifecycle_state": "RUNNING",
            "failure_policy": eff_policy,
            "load_ms": round(load_ms, 2),
            "model_identity": identity,
            "reason": "Kronos verified (SHA-256 + exact self-test identity) and running for active PAPER session.",
            "started_at": now_iso,
            "updated_at": now_iso,
        }
        with self._lock:
            self._attached_sessions[session_key] = rec
            self._session_worker_loaded = True
            self.config.model_loaded_preference = True
            self._last_error = None
            self._session_lifecycle_state = "RUNNING"
            self._session_lifecycle_reason = rec["reason"]
            if load_ms > 0:
                self._load_ms_samples.append(load_ms)

        self._emit_lifecycle_event(
            "KRONOS_READY",
            market=mkt,
            strategy=strat,
            session_id=session_id,
            detail={
                "lifecycle_state": "RUNNING",
                "load_ms": round(load_ms, 2),
                "model_identity": identity,
                "failure_policy": eff_policy,
            },
        )
        return dict(rec)

    def _record_session_start_failure(
        self,
        *,
        session_key: str,
        session_id: str,
        market: str,
        strategy: str,
        failure_policy: str,
        code: str,
        reason: str,
    ) -> dict[str, Any]:
        now_iso = _utc_now(self._clock).isoformat()
        rec = {
            "session_key": session_key,
            "session_id": session_id,
            "market": market,
            "strategy": strategy,
            "kronos_configured": True,
            "lifecycle_state": "FAILED",
            "failure_code": code,
            "failure_policy": failure_policy,
            "reason": redact_sensitive_data(reason),
            "started_at": now_iso,
            "updated_at": now_iso,
        }
        with self._lock:
            self._attached_sessions[session_key] = rec
            self._session_worker_loaded = False
            self._session_lifecycle_state = "FAILED"
            self._session_lifecycle_reason = str(rec["reason"])
            self._last_error = {
                "code": code,
                "message": str(rec["reason"]),
                "at": now_iso,
            }
        self._emit_lifecycle_event(
            "KRONOS_FAILED",
            market=market,
            strategy=strategy,
            session_id=session_id,
            severity="ERROR",
            detail=rec,
        )
        return dict(rec)

    def pause_for_paper_session(
        self,
        session_key: str,
        *,
        session_id: str = "",
        reason: str = "paper session paused",
    ) -> dict[str, Any]:
        now_iso = _utc_now(self._clock).isoformat()
        with self._lock:
            rec = self._attached_sessions.get(session_key)
            if rec is None:
                return {"session_key": session_key, "lifecycle_state": self._session_lifecycle_state}
            rec = {**rec, "paused": True, "updated_at": now_iso, "reason": reason}
            self._attached_sessions[session_key] = rec
            mkt = str(rec.get("market") or "LOCAL")
            strat = str(rec.get("strategy") or "")
            sid = session_id or str(rec.get("session_id") or "")
            any_running = any(
                s.get("kronos_configured") and s.get("lifecycle_state") == "RUNNING" and not s.get("paused")
                for s in self._attached_sessions.values()
            )
            if not any_running and self._session_worker_loaded:
                if self.kronos is not None:
                    stopper = getattr(self.kronos, "stop_session_worker", None)
                    if callable(stopper):
                        stopper()
                    self.kronos.close()
                self._session_worker_loaded = False
                self.config.model_loaded_preference = False
                self._session_lifecycle_state = "STOPPED"
                self._session_lifecycle_reason = f"Kronos paused/released ({reason})."
        self._emit_lifecycle_event(
            "KRONOS_STOPPED",
            market=mkt,
            strategy=strat,
            session_id=sid,
            detail={"lifecycle_state": self._session_lifecycle_state, "reason": reason, "paused": True},
        )
        return dict(rec)

    def stop_for_paper_session(
        self,
        session_key: str,
        *,
        session_id: str = "",
        reason: str = "paper session stopped",
    ) -> dict[str, Any]:
        """Release session attachment and cleanly stop Kronos worker when no session needs it."""
        now_iso = _utc_now(self._clock).isoformat()
        worker_released = False
        with self._lock:
            removed = self._attached_sessions.pop(session_key, None)
            mkt = str((removed or {}).get("market") or "LOCAL")
            strat = str((removed or {}).get("strategy") or "")
            sid = session_id or str((removed or {}).get("session_id") or "")
            active_configured = [
                s for s in self._attached_sessions.values()
                if s.get("kronos_configured") and not s.get("paused")
            ]
            if not active_configured:
                if self.kronos is not None:
                    stopper = getattr(self.kronos, "stop_session_worker", None)
                    if callable(stopper):
                        stopper()
                    self.kronos.close()
                worker_released = True
                self._session_worker_loaded = False
                self.config.model_loaded_preference = False
                self._session_lifecycle_state = "STOPPED"
                self._session_lifecycle_reason = f"Kronos stopped and worker resources released ({reason})."

        out = {
            "session_key": session_key,
            "session_id": sid,
            "lifecycle_state": self._session_lifecycle_state,
            "worker_released": worker_released,
            "active_configured_sessions": len(active_configured),
            "stopped_at": now_iso,
            "reason": reason,
        }
        if removed is not None and removed.get("kronos_configured"):
            self._emit_lifecycle_event(
                "KRONOS_STOPPED",
                market=mkt,
                strategy=strat,
                session_id=sid,
                detail=out,
            )
        return out

    def stop_all_sessions(self, *, reason: str = "shutdown or emergency stop") -> dict[str, Any]:
        with self._lock:
            keys = list(self._attached_sessions.keys())
        results = [self.stop_for_paper_session(k, reason=reason) for k in keys]
        if not keys:
            self.unload_model()
        return {"stopped_sessions": len(results), "lifecycle_state": self._session_lifecycle_state}

    def session_view(self, session_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            if session_key is not None and session_key in self._attached_sessions:
                return dict(self._attached_sessions[session_key])
            return {
                "lifecycle_state": self._session_lifecycle_state,
                "reason": self._session_lifecycle_reason,
                "active_sessions": [dict(v) for v in self._attached_sessions.values()],
            }

    # ------------------------------------------------------------------
    # Validation & Bounded Inference
    # ------------------------------------------------------------------

    def _derive_provider_state(self, base_view: dict[str, Any]) -> tuple[str, str]:
        op_state = (base_view.get("operation") or {}).get("state")
        if self._active_inferences > 0 or op_state == "RUNNING":
            return "RUNNING", "Kronos operation or CPU inference currently running."
        if self._last_error is not None:
            return "ERROR", str(self._last_error.get("message") or "Kronos inference error")
        if op_state == "ERROR":
            err_msg = ((base_view.get("operation") or {}).get("error") or {}).get("message")
            return "ERROR", str(err_msg or "Kronos setup/self-test reported an error")
        issues = list(base_view.get("issues") or [])
        if any("MISMATCH" in str(i).upper() or "CORRUPT" in str(i).upper() or "INVALID" in str(i).upper() for i in issues):
            return "ERROR", "; ".join(str(i) for i in issues)
        if base_view.get("status") == "INFERENCE_ERROR":
            return "ERROR", "; ".join(str(i) for i in issues) or "Kronos inference failed; non-Kronos fallback required."

        art_verified = bool(base_view.get("artifact_verified"))
        runtime_ready = base_view.get("inference_runtime") == "READY"
        avail = bool(base_view.get("available_for_inference"))
        if not art_verified and not runtime_ready:
            return (
                "NOT_INSTALLED",
                "Kronos-small safetensors (~109.5 MiB) and locked CPU runtime are not installed in the per-user directory.",
            )
        if not avail:
            missing_parts = []
            if not art_verified:
                missing_parts.append("pinned model/tokenizer safetensors")
            if not runtime_ready:
                missing_parts.append("locked CPython 3.11/3.12 CPU runtime")
            return (
                "NOT_INSTALLED" if any("MISSING" in str(i).upper() for i in issues) else "NOT_VERIFIED",
                f"Incomplete or unverified Kronos components ({', '.join(missing_parts)}). Run Install Pinned Kronos or Verify.",
            )
        if not _self_test_passed(base_view):
            return (
                "NOT_VERIFIED",
                "Artifacts and CPU runtime match SHA-256 pins, but offline genuine CPU self-test has not yet run for this interpreter.",
            )
        if self._forecasts:
            now = _utc_now(self._clock)
            newest = max(
                self._forecasts.values(),
                key=lambda item: str(item.get("generated_at") or ""),
            )
            gen_ts = parse_timestamp(str(newest["generated_at"]))
            if (now - gen_ts).total_seconds() > self.config.max_forecast_age_seconds:
                return "STALE", "Last cached Kronos forecast exceeded max_forecast_age_seconds; request a fresh bar forecast."
        return "READY", "Pinned Kronos-small + Tokenizer verified on CPU (mock_outputs_used=false)."

    def _derive_lifecycle_state(self, base_view: dict[str, Any], provider_state: str) -> tuple[str, str]:
        # Check if warm session worker crashed behind the scenes
        sess_worker = base_view.get("session_worker") or {}
        if (
            self._session_worker_loaded
            and isinstance(sess_worker, dict)
            and "alive" in sess_worker
            and sess_worker.get("alive") is False
            and sess_worker.get("exit_code") not in (None, 0)
        ):
            self._session_worker_loaded = False
            self._session_lifecycle_state = "FAILED"
            self._session_lifecycle_reason = (
                f"Kronos CPU worker exited unexpectedly (exit_code={sess_worker.get('exit_code')})."
            )

        if self._active_inferences > 0:
            return "RUNNING", "Bounded Kronos CPU inference actively executing."

        if self._session_lifecycle_state in {"LOADING", "RUNNING", "DEGRADED", "FAILED", "STOPPED"}:
            # Never falsely report RUNNING if provider hit an error or lost verification
            if self._session_lifecycle_state == "RUNNING" and provider_state in {"ERROR", "NOT_INSTALLED", "NOT_VERIFIED"}:
                return "FAILED", f"Kronos left RUNNING due to provider state {provider_state}."
            if self._session_lifecycle_state == "RUNNING" and provider_state == "STALE":
                return "DEGRADED", "Active session forecast exceeded max_forecast_age_seconds (STALE)."
            return self._session_lifecycle_state, self._session_lifecycle_reason

        if provider_state == "READY":
            return "READY", "Verified and self-tested; ready to load when a PAPER session starts."
        if provider_state == "STALE":
            return "DEGRADED", "Verified, but latest cached forecast is STALE."
        if provider_state == "ERROR":
            return "FAILED", str((self._last_error or {}).get("message") or "Kronos reported an error.")
        return "NOT_CONFIGURED", "Kronos optional runtime/weights not configured or self-tested yet."

    def validate_input_bars(
        self,
        *,
        market: str,
        symbol: str,
        timeframe: str,
        bars: Iterable[Any],
        require_fresh: bool = False,
    ) -> list[Candle]:
        mkt = (market or "").strip().upper()
        if mkt not in SUPPORTED_MARKETS:
            raise KronosInputError(f"Unsupported market {market!r}; expected NSE or NASDAQ")
        sym = (symbol or "").strip().upper()
        if not sym or len(sym) > 64:
            raise KronosInputError("Symbol is required for Kronos inference")
        tf_raw = (timeframe or "").strip()
        tf_norm = "1D" if tf_raw.lower() == "1d" else tf_raw
        if tf_norm not in TIMEFRAMES:
            raise KronosInputError(f"Unsupported timeframe {timeframe!r}; expected one of {sorted(TIMEFRAMES)}")

        normalized: list[Candle] = []
        for idx, raw in enumerate(bars):
            if isinstance(raw, Candle):
                c = raw
            elif isinstance(raw, dict):
                try:
                    o = float(raw["open"])
                    h = float(raw["high"])
                    lo = float(raw["low"])
                    cl = float(raw["close"])
                    vol = float(raw.get("volume", 0.0))
                except (KeyError, TypeError, ValueError) as exc:
                    raise KronosInputError(f"Bar {idx} has missing or non-numeric OHLCV fields") from exc
                if not all(math.isfinite(x) for x in (o, h, lo, cl, vol)):
                    raise KronosInputError(f"Bar {idx} contains NaN or infinite values")
                if min(o, h, lo, cl) <= 0 or vol < 0:
                    raise KronosInputError(f"Bar {idx} has non-positive price or negative volume")
                if h < max(o, cl) or lo > min(o, cl):
                    raise KronosInputError(f"Bar {idx} violates OHLC envelope (high < open/close or low > open/close)")
                c = Candle(
                    timestamp=parse_timestamp(str(raw["timestamp"])),
                    open=o,
                    high=h,
                    low=lo,
                    close=cl,
                    volume=vol,
                    symbol=sym.replace("|", "-"),
                )
            else:
                raise KronosInputError(f"Unsupported bar object at index {idx}")

            if not all(math.isfinite(x) for x in (c.open, c.high, c.low, c.close, float(c.volume))):
                raise KronosInputError(f"Bar {idx} contains non-finite OHLCV values")
            if min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
                raise KronosInputError(f"Bar {idx} contains non-positive price or negative volume")
            normalized.append(c)

        if len(normalized) < self.config.min_context_bars:
            raise KronosInputError(
                f"At least {self.config.min_context_bars} completed bars are required for Kronos context (received {len(normalized)})"
            )
        for idx in range(1, len(normalized)):
            if normalized[idx].timestamp <= normalized[idx - 1].timestamp:
                raise KronosInputError(
                    f"Input bars must be strictly chronological and deduplicated (violation at index {idx})"
                )

        if require_fresh:
            now = _utc_now(self._clock)
            age_s = (now - normalized[-1].timestamp).total_seconds()
            if age_s > self.config.max_bar_staleness_seconds:
                raise KronosInputError(
                    f"Latest completed bar ({normalized[-1].timestamp.isoformat()}) is stale ({age_s:.0f}s > {self.config.max_bar_staleness_seconds}s)"
                )
            if age_s < -60:
                raise KronosInputError("Latest bar timestamp is in the future (look-ahead/clock error)")

        return normalized[-self.config.max_context_bars :]

    def validate_forecast_output(
        self,
        forecast: Any,
        *,
        validated_bars: list[Candle],
        expected_horizon: int,
    ) -> dict[str, float]:
        """Validate returned Forecast object finiteness, quantile monotonicity, horizon, and window."""
        if forecast is None:
            raise KronosOutputValidationError("Kronos worker returned null forecast")
        for attr in ("mean_close", "q10_close", "q90_close", "up_probability", "horizon", "input_end"):
            if not hasattr(forecast, attr):
                raise KronosOutputValidationError(f"Kronos forecast missing required attribute {attr!r}")
        if getattr(forecast, "mock_outputs_used", False) is True:
            raise KronosOutputValidationError("Mock forecast outputs are strictly forbidden")

        mean_c = float(forecast.mean_close)
        q10_c = float(forecast.q10_close)
        q90_c = float(forecast.q90_close)
        up_p = float(forecast.up_probability)
        if not all(math.isfinite(v) for v in (mean_c, q10_c, q90_c, up_p)):
            raise KronosOutputValidationError("Non-finite Kronos forecast rejected")
        if mean_c <= 0.0 or q10_c <= 0.0 or q90_c <= 0.0:
            raise KronosOutputValidationError("Non-positive Kronos price forecast rejected")
        eps = 1e-6
        if q10_c > q90_c + eps or q10_c > mean_c + eps or mean_c > q90_c + eps:
            raise KronosOutputValidationError(
                f"Malformed Kronos quantile ordering rejected (q10={q10_c}, mean={mean_c}, q90={q90_c})"
            )
        if not (0.0 <= up_p <= 1.0):
            raise KronosOutputValidationError(f"Out-of-bounds Kronos up_probability={up_p} rejected")
        if int(forecast.horizon) != int(expected_horizon):
            raise KronosOutputValidationError(
                f"Kronos horizon mismatch (expected {expected_horizon}, got {forecast.horizon})"
            )
        if forecast.input_end != validated_bars[-1].timestamp:
            raise KronosOutputValidationError(
                "Kronos forecast input_end does not match latest validated completed bar timestamp"
            )
        if not str(getattr(forecast, "artifact_digest", "") or "").strip():
            raise KronosOutputValidationError("Kronos forecast missing artifact_digest identity")

        last_close = float(validated_bars[-1].close)
        expected_return_pct = ((mean_c / last_close) - 1.0) * 100.0
        band_pct = ((q90_c - q10_c) / last_close) * 100.0
        directional_confidence = max(up_p, 1.0 - up_p)
        directional_dispersion = abs(up_p - 0.5) * 2.0
        uncertainty_penalty = min(
            0.25,
            max(0.0, band_pct / max(2.0, float(self.config.max_uncertainty_pct) * 4.0)),
        )
        effective_confidence = max(0.0, min(1.0, directional_confidence - uncertainty_penalty))
        return {
            "last_close": last_close,
            "mean_close": mean_c,
            "q10_close": q10_c,
            "q90_close": q90_c,
            "up_probability": up_p,
            "expected_return_pct": expected_return_pct,
            "uncertainty_pct": band_pct,
            "directional_confidence": directional_confidence,
            "directional_dispersion": directional_dispersion,
            "effective_confidence": effective_confidence,
        }

    def predict(
        self,
        *,
        market: str,
        symbol: str,
        timeframe: str,
        bars: Iterable[Any],
        horizon: int | None = None,
        samples: int | None = None,
        require_fresh: bool = False,
        allow_when_toggle_off: bool = False,
    ) -> dict[str, Any]:
        """Run genuine Kronos CPU inference on validated completed bars; fail closed on any error."""
        tf_raw = (timeframe or "").strip()
        tf_norm = "1D" if tf_raw.lower() == "1d" else tf_raw
        validated_bars = self.validate_input_bars(
            market=market,
            symbol=symbol,
            timeframe=tf_norm,
            bars=bars,
            require_fresh=require_fresh,
        )
        mkt = market.strip().upper()
        sym = symbol.strip().upper()
        use_horizon = int(horizon or self.config.horizon)
        use_samples = int(samples or self.config.samples)
        if not (1 <= use_horizon <= 60) or not (4 <= use_samples <= 32):
            raise KronosInputError("Invalid forecast horizon or sample count")

        with self._lock:
            if not allow_when_toggle_off and not self.config.inference_enabled:
                raise KronosUnavailableError(
                    "Kronos inference is currently DISABLED in Model Lab; enable forecasting first."
                )
            if self.kronos is None:
                self._last_error = {"code": "UNAVAILABLE", "message": "KronosService unavailable"}
                raise KronosUnavailableError("KronosService unavailable")
            base = self.kronos.status()
            if not base.get("available_for_inference"):
                base = self.kronos.verify()
            if not base.get("available_for_inference"):
                raise KronosUnavailableError(
                    "Kronos is NOT_INSTALLED or NOT_VERIFIED; genuine weights and CPU runtime are required (mock fallback forbidden)."
                )
            if not _self_test_passed(base):
                raise KronosUnavailableError(
                    "Kronos is NOT_VERIFIED: offline CPU self-test has not PASSED for the exact configured runtime/artifact identity."
                )
            self._active_inferences += 1

        ikey = f"{mkt}:{sym}"
        wall_t0 = time.perf_counter()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kronos-infer")
        future = pool.submit(
            self.kronos.forecast,
            ikey,
            validated_bars,
            timeframe=tf_norm,
            horizon=use_horizon,
            samples=use_samples,
            validated=False,
            validation_id="",
            input_label=f"MODEL_LAB_{mkt}_COMPLETED_BARS",
        )
        try:
            forecast = future.result(timeout=self.config.timeout_seconds)
        except FuturesTimeoutError as exc:
            if self.kronos is not None:
                stopper = getattr(self.kronos, "stop_session_worker", None)
                if callable(stopper):
                    stopper()
                self.kronos.close()
            with self._lock:
                self._timeout_count += 1
                self._session_worker_loaded = False
                self._last_error = {
                    "code": "INFERENCE_TIMEOUT",
                    "message": f"Kronos inference exceeded {self.config.timeout_seconds:.1f}s timeout; worker terminated.",
                    "at": _utc_now(self._clock).isoformat(),
                }
            raise KronosUnavailableError(self._last_error["message"]) from exc
        except Exception as exc:
            with self._lock:
                self._last_error = {
                    "code": type(exc).__name__,
                    "message": redact_sensitive_data(
                        f"Kronos inference failed closed ({type(exc).__name__}): {str(exc)[:180]}"
                    ),
                    "at": _utc_now(self._clock).isoformat(),
                }
            raise KronosUnavailableError(self._last_error["message"]) from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            with self._lock:
                self._active_inferences = max(0, self._active_inferences - 1)

        wall_ms = round((time.perf_counter() - wall_t0) * 1000.0, 2)
        try:
            metrics = self.validate_forecast_output(
                forecast,
                validated_bars=validated_bars,
                expected_horizon=use_horizon,
            )
        except KronosOutputValidationError as exc:
            with self._lock:
                self._invalid_output_count += 1
                self._last_error = {
                    "code": "INVALID_OUTPUT",
                    "message": str(exc),
                    "at": _utc_now(self._clock).isoformat(),
                }
            raise

        last_inf = (self.kronos.status().get("last_inference") or {}) if self.kronos else {}
        load_ms = float(last_inf.get("load_ms") or 0.0)
        reported_infer_ms = float(last_inf.get("inference_ms") or 0.0)
        infer_ms = reported_infer_ms if reported_infer_ms > 0.0 else max(0.01, wall_ms)
        now = _utc_now(self._clock)

        record = {
            "schema": "axiom.model_lab.forecast.v1",
            "is_forecast": True,
            "advisory_only": True,
            "trade_instruction": False,
            "execution_authorized": False,
            "certainty_claim": "none",
            "mock_outputs_used": False,
            "market": mkt,
            "symbol": sym,
            "instrument_key": ikey,
            "timeframe": tf_norm,
            "context_bars_used": len(validated_bars),
            "input_start": validated_bars[0].timestamp.isoformat(),
            "input_end": validated_bars[-1].timestamp.isoformat(),
            "last_close": round(metrics["last_close"], 4),
            "generated_at": now.isoformat(),
            "horizon_bars": use_horizon,
            "samples": use_samples,
            "mean_close": round(metrics["mean_close"], 4),
            "q10_close": round(metrics["q10_close"], 4),
            "q90_close": round(metrics["q90_close"], 4),
            "expected_return_pct": round(metrics["expected_return_pct"], 4),
            "uncertainty_band_q10_q90_pct": round(metrics["uncertainty_pct"], 4),
            "up_probability": round(metrics["up_probability"], 4),
            "directional_confidence": round(metrics["directional_confidence"], 4),
            "effective_confidence": round(metrics["effective_confidence"], 4),
            "directional_dispersion_score": round(metrics["directional_dispersion"], 4),
            "calibration_note": "Monte Carlo sample quantile dispersion across autoregressive token rollouts; uncalibrated to out-of-sample hit-rate unless verified in Research Runs.",
            "model_identity": {
                "model_repository": (self.kronos.manifest.get("model") or {}).get("repository", "NeoQuasar/Kronos-small"),
                "model_revision": forecast.model_revision,
                "tokenizer_repository": (self.kronos.manifest.get("tokenizer") or {}).get("repository", "NeoQuasar/Kronos-Tokenizer-base"),
                "tokenizer_revision": forecast.tokenizer_revision,
                "manifest_sha256": forecast.artifact_digest,
                "license": "MIT (shiyu-coder/Kronos)",
                "device": "cpu",
            },
            "latency_ms": {
                "load_ms": round(load_ms, 2),
                "inference_ms": round(infer_ms, 2),
                "wall_clock_ms": wall_ms,
            },
            "freshness_state": "FRESH",
        }
        with self._lock:
            self._last_error = None
            self._last_inference_at = now.isoformat()
            self._last_latency_ms = round(infer_ms, 2)
            self._forecast_produced_count += 1
            self._last_produced_at = now.isoformat()
            self._last_produced_summary = {
                "market": mkt,
                "symbol": sym,
                "timeframe": tf_norm,
                "generated_at": now.isoformat(),
                "input_end": record["input_end"],
                "up_probability": record["up_probability"],
                "expected_return_pct": record["expected_return_pct"],
                "effective_confidence": record["effective_confidence"],
                "uncertainty_band_q10_q90_pct": record["uncertainty_band_q10_q90_pct"],
                "latency_ms": round(infer_ms, 2),
            }
            if load_ms > 0:
                self._load_ms_samples.append(load_ms)
            if infer_ms > 0:
                self._infer_ms_samples.append(infer_ms)
            self._forecasts[f"{mkt}:{sym}:{tf_norm}"] = record
        return record

    def predict_for_session(
        self,
        session_key: str,
        *,
        session_id: str = "",
        decision_id: str = "",
        market: str,
        symbol: str,
        timeframe: str,
        bars: Iterable[Any],
        horizon: int | None = None,
        samples: int | None = None,
    ) -> dict[str, Any]:
        """Bounded, structured PAPER-session inference returning status + telemetry without silent swallow."""
        mkt = (market or "LOCAL").strip().upper()
        sym = (symbol or "").strip().upper()
        with self._lock:
            sess = dict(self._attached_sessions.get(session_key) or {})
        strat = str(sess.get("strategy") or "")
        sid = session_id or str(sess.get("session_id") or "")

        if not sess or not sess.get("kronos_configured"):
            return {
                "ok": False,
                "status": "NOT_CONFIGURED",
                "forecast_produced": False,
                "forecast": None,
                "error_code": "NOT_CONFIGURED",
                "error_reason": "Kronos is not configured for this PAPER session.",
                "failure_code": "NOT_CONFIGURED",
                "failure_reason": "Kronos is not configured for this PAPER session.",
                "latency_ms": None,
                "lifecycle_state": "NOT_CONFIGURED",
            }
        if sess.get("lifecycle_state") == "FAILED":
            f_code = str(sess.get("failure_code") or "SESSION_START_FAILED")
            f_reason = str(sess.get("reason") or "Kronos failed session readiness verification.")
            return {
                "ok": False,
                "status": "UNAVAILABLE",
                "forecast_produced": False,
                "forecast": None,
                "error_code": f_code,
                "error_reason": f_reason,
                "failure_code": f_code,
                "failure_reason": f_reason,
                "latency_ms": None,
                "lifecycle_state": "FAILED",
            }

        t0 = time.perf_counter()
        try:
            fc = self.predict(
                market=mkt,
                symbol=sym,
                timeframe=timeframe,
                bars=bars,
                horizon=horizon,
                samples=samples,
                allow_when_toggle_off=True,
            )
            lat = float((fc.get("latency_ms") or {}).get("inference_ms") or round((time.perf_counter() - t0) * 1000.0, 2))
            with self._lock:
                if session_key in self._attached_sessions:
                    self._attached_sessions[session_key]["lifecycle_state"] = "RUNNING"
                    self._attached_sessions[session_key]["produced_count"] = int(self._attached_sessions[session_key].get("produced_count") or 0) + 1
                    self._attached_sessions[session_key]["updated_at"] = _utc_now(self._clock).isoformat()
                self._session_lifecycle_state = "RUNNING"
                self._session_lifecycle_reason = f"Kronos forecast produced for {mkt}:{sym} ({lat:.1f} ms)."
            self._emit_lifecycle_event(
                "KRONOS_FORECAST_PRODUCED",
                market=mkt,
                symbol=sym,
                strategy=strat,
                session_id=sid,
                decision_id=decision_id,
                detail={
                    "lifecycle_state": "RUNNING",
                    "latency_ms": round(lat, 2),
                    "input_start": fc["input_start"],
                    "input_end": fc["input_end"],
                    "context_bars_used": fc["context_bars_used"],
                    "up_probability": fc["up_probability"],
                    "expected_return_pct": fc["expected_return_pct"],
                    "effective_confidence": fc["effective_confidence"],
                    "uncertainty_band_q10_q90_pct": fc["uncertainty_band_q10_q90_pct"],
                    "manifest_sha256": fc["model_identity"]["manifest_sha256"],
                },
            )
            return {
                "ok": True,
                "status": "OK",
                "forecast_produced": True,
                "forecast": fc,
                "error_code": None,
                "error_reason": None,
                "failure_code": None,
                "failure_reason": None,
                "latency_ms": round(lat, 2),
                "lifecycle_state": "RUNNING",
            }
        except KronosInputError as exc:
            lat = round((time.perf_counter() - t0) * 1000.0, 2)
            reason = redact_sensitive_data(str(exc))
            with self._lock:
                self._session_lifecycle_state = "DEGRADED"
                self._session_lifecycle_reason = f"Input validation rejected bars for {mkt}:{sym}: {reason}"
                if session_key in self._attached_sessions:
                    self._attached_sessions[session_key]["lifecycle_state"] = "DEGRADED"
                    self._attached_sessions[session_key]["reason"] = self._session_lifecycle_reason
            self._emit_lifecycle_event(
                "KRONOS_INFER_INVALID",
                market=mkt,
                symbol=sym,
                strategy=strat,
                session_id=sid,
                decision_id=decision_id,
                severity="ERROR",
                detail={"lifecycle_state": "DEGRADED", "latency_ms": lat, "error_code": "INVALID_INPUT", "reason": reason},
            )
            return {
                "ok": False,
                "status": "INVALID_INPUT",
                "forecast_produced": False,
                "forecast": None,
                "error_code": "INVALID_INPUT",
                "error_reason": str(reason),
                "failure_code": "INVALID_INPUT",
                "failure_reason": str(reason),
                "latency_ms": lat,
                "lifecycle_state": "DEGRADED",
            }
        except KronosOutputValidationError as exc:
            lat = round((time.perf_counter() - t0) * 1000.0, 2)
            reason = redact_sensitive_data(str(exc))
            with self._lock:
                self._session_lifecycle_state = "DEGRADED"
                self._session_lifecycle_reason = f"Output validation rejected forecast for {mkt}:{sym}: {reason}"
                if session_key in self._attached_sessions:
                    self._attached_sessions[session_key]["lifecycle_state"] = "DEGRADED"
                    self._attached_sessions[session_key]["reason"] = self._session_lifecycle_reason
            self._emit_lifecycle_event(
                "KRONOS_INFER_INVALID",
                market=mkt,
                symbol=sym,
                strategy=strat,
                session_id=sid,
                decision_id=decision_id,
                severity="ERROR",
                detail={"lifecycle_state": "DEGRADED", "latency_ms": lat, "error_code": "INVALID_OUTPUT", "reason": reason},
            )
            return {
                "ok": False,
                "status": "INVALID_OUTPUT",
                "forecast_produced": False,
                "forecast": None,
                "error_code": "INVALID_OUTPUT",
                "error_reason": str(reason),
                "failure_code": "INVALID_OUTPUT",
                "failure_reason": str(reason),
                "latency_ms": lat,
                "lifecycle_state": "DEGRADED",
            }
        except KronosUnavailableError as exc:
            lat = round((time.perf_counter() - t0) * 1000.0, 2)
            msg = str(redact_sensitive_data(str(exc)))
            is_timeout = "timeout" in msg.lower()
            sess_pol = str(sess.get("failure_policy") or self.config.failure_policy)
            next_state = (
                "DEGRADED"
                if (is_timeout or sess_pol == "EXPLICIT_NON_KRONOS_FALLBACK")
                else "FAILED"
            )
            code = "INFERENCE_TIMEOUT" if is_timeout else "WORKER_UNAVAILABLE"
            with self._lock:
                self._session_lifecycle_state = next_state
                self._session_lifecycle_reason = msg
                if session_key in self._attached_sessions:
                    self._attached_sessions[session_key]["lifecycle_state"] = next_state
                    self._attached_sessions[session_key]["reason"] = msg
            self._emit_lifecycle_event(
                "KRONOS_INFER_TIMEOUT" if is_timeout else "KRONOS_FAILED",
                market=mkt,
                symbol=sym,
                strategy=strat,
                session_id=sid,
                decision_id=decision_id,
                severity="WARN" if is_timeout else "ERROR",
                detail={"lifecycle_state": next_state, "latency_ms": lat, "error_code": code, "reason": msg},
            )
            if next_state == "DEGRADED":
                self._emit_lifecycle_event(
                    "KRONOS_DEGRADED",
                    market=mkt,
                    symbol=sym,
                    strategy=strat,
                    session_id=sid,
                    decision_id=decision_id,
                    severity="WARN",
                    detail={"lifecycle_state": "DEGRADED", "reason": msg},
                )
            return {
                "ok": False,
                "status": "TIMEOUT" if is_timeout else "FAILED",
                "forecast_produced": False,
                "forecast": None,
                "error_code": code,
                "error_reason": msg,
                "failure_code": code,
                "failure_reason": msg,
                "latency_ms": lat,
                "lifecycle_state": next_state,
            }

    def session_view(self, session_key: str) -> dict[str, Any]:
        with self._lock:
            sess = self._attached_sessions.get(session_key)
            if sess is None:
                return {
                    "session_key": session_key,
                    "kronos_configured": False,
                    "lifecycle_state": "NOT_CONFIGURED",
                    "failure_policy": self.config.failure_policy,
                    "produced_count": 0,
                    "consumed_count": 0,
                }
            return dict(sess)

    def record_forecast_consumption(
        self,
        session_key: str = "",
        *,
        session_id: str = "",
        decision_id: str = "",
        market: str = "",
        symbol: str = "",
        timeframe: str = "",
        strategy: str = "",
        forecast: dict[str, Any] | None = None,
        final_action: str = "",
        contribution_detail: str = "",
        reason: str = "",
        consumed: bool = True,
    ) -> dict[str, Any]:
        """Record proof that an active PAPER strategy decision actually consumed a valid forecast."""
        now_iso = _utc_now(self._clock).isoformat()
        fc = forecast if isinstance(forecast, dict) else {}
        detail_txt = contribution_detail or reason
        summary = {
            "session_key": session_key,
            "session_id": session_id,
            "decision_id": decision_id,
            "market": market.upper(),
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "strategy": strategy,
            "consumed_at": now_iso,
            "forecast_generated_at": fc.get("generated_at"),
            "input_end": fc.get("input_end"),
            "manifest_sha256": (fc.get("model_identity") or {}).get("manifest_sha256"),
            "final_action": final_action,
            "contribution_detail": detail_txt,
        }
        if consumed:
            with self._lock:
                self._forecast_consumed_count += 1
                self._last_consumed_at = now_iso
                self._last_consumed_summary = summary
                if session_key and session_key in self._attached_sessions:
                    self._attached_sessions[session_key]["consumed_count"] = (
                        int(self._attached_sessions[session_key].get("consumed_count") or 0) + 1
                    )
            self._emit_lifecycle_event(
                "KRONOS_FORECAST_CONSUMED",
                market=market.upper(),
                symbol=symbol.upper(),
                strategy=strategy,
                session_id=session_id,
                decision_id=decision_id,
                detail={
                    "final_action": final_action,
                    "contribution_detail": detail_txt,
                    "forecast_generated_at": fc.get("generated_at"),
                    "manifest_sha256": (fc.get("model_identity") or {}).get("manifest_sha256"),
                },
            )
        return summary

    def get_cached_forecast(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        *,
        latest_bar_ts: datetime | None = None,
    ) -> dict[str, Any] | None:
        tf_raw = (timeframe or "").strip()
        tf_norm = "1D" if tf_raw.lower() == "1d" else tf_raw
        key = f"{market.strip().upper()}:{symbol.strip().upper()}:{tf_norm}"
        with self._lock:
            rec = self._forecasts.get(key)
            if rec is None:
                return None
            out = dict(rec)
            now = _utc_now(self._clock)
            age_s = max(0.0, (now - parse_timestamp(str(out["generated_at"]))).total_seconds())
            stale = age_s > self.config.max_forecast_age_seconds
            if latest_bar_ts is not None and parse_timestamp(str(out["input_end"])) != latest_bar_ts:
                stale = True
            out["age_seconds"] = round(age_s, 1)
            out["freshness_state"] = "STALE" if stale else "FRESH"
            return out

    def clear_error(self) -> dict[str, Any]:
        with self._lock:
            self._last_error = None
            if self.kronos is not None:
                self.kronos.verify()
            if self._session_lifecycle_state in {"FAILED", "DEGRADED"} and not self._attached_sessions:
                self._session_lifecycle_state = "NOT_CONFIGURED"
                self._session_lifecycle_reason = "Error cleared by operator."
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            base: dict[str, Any] = {}
            install_plan: dict[str, Any] = {}
            if self.setup_manager is not None:
                base = self.setup_manager.status()
            elif self.kronos is not None:
                base = self.kronos.status()
            if self.kronos is not None:
                try:
                    install_plan = self.kronos.install_plan()
                except ArtifactError:
                    install_plan = {}

            p_state, summary = self._derive_provider_state(base)
            lc_state, lc_reason = self._derive_lifecycle_state(base, p_state)
            loads_sorted = sorted(self._load_ms_samples)
            infers_sorted = sorted(self._infer_ms_samples)
            now = _utc_now(self._clock)
            recent_forecasts = []
            for item in self._forecasts.values():
                copy_item = dict(item)
                age_s = max(0.0, (now - parse_timestamp(str(copy_item["generated_at"]))).total_seconds())
                copy_item["age_seconds"] = round(age_s, 1)
                copy_item["freshness_state"] = (
                    "STALE" if age_s > self.config.max_forecast_age_seconds else "FRESH"
                )
                recent_forecasts.append(copy_item)
            recent_forecasts.sort(key=lambda r: str(r.get("generated_at") or ""), reverse=True)

            art_verified = bool(base.get("artifact_verified"))
            runtime_ready = base.get("inference_runtime") == "READY"
            st_passed = _self_test_passed(base)
            model_loaded = bool(
                (self._session_worker_loaded or self.config.model_loaded_preference)
                and p_state in {"READY", "RUNNING", "STALE"}
            )
            active_sessions = [dict(v) for v in self._attached_sessions.values()]
            running_sessions = [
                s for s in active_sessions
                if s.get("kronos_configured") and s.get("lifecycle_state") == "RUNNING" and not s.get("paused")
            ]

            is_running = len(running_sessions) > 0 and lc_state == "RUNNING"
            is_degraded = lc_state == "DEGRADED" or p_state == "STALE"
            is_failed = lc_state == "FAILED" or p_state == "ERROR"
            truth_matrix = {
                "installed": bool(art_verified and runtime_ready),
                "artifacts_verified": art_verified,
                "runtime_ready": runtime_ready,
                "self_test_passed": st_passed,
                "validated": bool(art_verified and runtime_ready and st_passed),
                "loaded": model_loaded,
                "model_loaded": model_loaded,
                "running": is_running,
                "session_running": is_running,
                "degraded": is_degraded,
                "failed": is_failed,
                "lifecycle_state": lc_state,
                "forecast_produced": self._forecast_produced_count > 0,
                "produced_count": self._forecast_produced_count,
                "forecast_produced_count": self._forecast_produced_count,
                "last_forecast_produced_at": self._last_produced_at,
                "last_produced_summary": self._last_produced_summary,
                "forecast_consumed": self._forecast_consumed_count > 0,
                "consumed_count": self._forecast_consumed_count,
                "forecast_consumed_count": self._forecast_consumed_count,
                "last_forecast_consumed_at": self._last_consumed_at,
                "last_consumed_summary": self._last_consumed_summary,
            }

            return {
                "provider_state": p_state,
                "provider_states_supported": list(KRONOS_PROVIDER_STATES),
                "lifecycle_state": lc_state,
                "lifecycle_states_supported": list(KRONOS_LIFECYCLE_STATES),
                "lifecycle_reason": lc_reason,
                "summary": summary,
                "inference_enabled": self.config.inference_enabled,
                "model_loaded": model_loaded,
                "last_error": self._last_error,
                "truth_matrix": truth_matrix,
                "paper_policy": self.paper_policy_view(),
                "active_paper_sessions": active_sessions,
                "execution_authorization_effect": "NONE — forecasts never create, arm, or activate trading",
                "fine_tuning_policy": "DISABLED — Kronos weights are frozen SHA-256-verified foundation artifacts; AXIOM trains separate decision models and strategy parameters.",
                "license": "MIT License (Copyright (c) 2025 ShiYu — shiyu-coder/Kronos)",
                "config": asdict(self.config),
                "diagnostics": {
                    "load_ms_p50": _percentile(loads_sorted, 50.0),
                    "load_ms_p95": _percentile(loads_sorted, 95.0),
                    "inference_ms_p50": _percentile(infers_sorted, 50.0),
                    "inference_ms_p95": _percentile(infers_sorted, 95.0),
                    "last_inference_at": self._last_inference_at,
                    "last_latency_ms": self._last_latency_ms,
                    "inference_count": len(infers_sorted),
                    "forecast_produced_count": self._forecast_produced_count,
                    "forecast_consumed_count": self._forecast_consumed_count,
                    "timeout_count": self._timeout_count,
                    "invalid_output_count": self._invalid_output_count,
                    "process_rss_mb": _process_rss_mb(),
                    "expected_weights_mb": round(float(install_plan.get("expected_total_bytes") or 114_823_553) / (1024 * 1024), 2),
                    "device": "cpu",
                    "mock_outputs_used": False,
                    "last_error": self._last_error,
                },
                "kronos_engine": base,
                "install_plan": install_plan,
                "recent_forecasts": recent_forecasts[:12],
            }
