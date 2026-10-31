"""Kronos Forecast Provider for NSE & NASDAQ completed OHLCV bars.

Wraps AXIOM's pinned ``KronosService`` (NeoQuasar/Kronos-small +
Kronos-Tokenizer-base, locked CPU runtime, MIT upstream source) with:
- Six explicit states: ``NOT_INSTALLED``, ``NOT_VERIFIED``, ``READY``,
  ``RUNNING``, ``ERROR``, ``STALE``
- Forecasting-only Load/Unload and Enable/Disable controls (never touch
  trading authorization or execution state)
- Strict input validation (market, symbol, timeframe, chronological ordering,
  NaN/non-positive rejection, staleness, minimum context)
- Bounded timeout, cancellation, sanitized error diagnostics, and rolling
  p50/p95 load & inference latency + resource telemetry.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.autonomy.kronos import KronosService
from app.autonomy.kronos_artifacts import ArtifactError
from app.data.models import TIMEFRAMES, Candle, parse_timestamp

KRONOS_PROVIDER_STATES = (
    "NOT_INSTALLED",
    "NOT_VERIFIED",
    "READY",
    "RUNNING",
    "ERROR",
    "STALE",
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
    """Best-effort current process RSS in MiB (POSIX /proc or resource; Windows ctypes)."""
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


class KronosInputError(ValueError):
    """Invalid symbol, market, timeframe, ordering, NaN, or context bars."""


class KronosUnavailableError(RuntimeError):
    """Fail-closed error when Kronos cannot produce a verified real forecast."""


class KronosForecastProvider:
    """Well-defined, fail-closed Kronos inference interface for Model Lab & Paper."""

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

    def set_inference_enabled(self, enabled: bool) -> dict[str, Any]:
        """Toggle forecasting only; never alters paper/live trading authorization."""
        with self._lock:
            self.config.inference_enabled = bool(enabled)
            self._save_config()
            return self.status()

    def load_model(self) -> dict[str, Any]:
        """Verify artifacts/runtime and mark provider ready for on-demand inference."""
        with self._lock:
            if self.kronos is None:
                self._last_error = {"code": "SERVICE_MISSING", "message": "KronosService is not initialized"}
                raise KronosUnavailableError("KronosService is not initialized")
            verified = self.kronos.verify()
            if not verified.get("available_for_inference"):
                issues = "; ".join(verified.get("issues") or ["Kronos artifacts or CPU runtime not installed"])
                raise KronosUnavailableError(f"Cannot load Kronos: {issues}")
            self.config.model_loaded_preference = True
            self._last_error = None
            return self.status()

    def unload_model(self) -> dict[str, Any]:
        """Terminate any active worker subprocess and mark Kronos unloaded."""
        with self._lock:
            self.config.model_loaded_preference = False
            if self.kronos is not None:
                self.kronos.close()
            return self.status()

    def cancel_active(self) -> dict[str, Any]:
        """Cancel active setup/self-test or subprocess inference without touching trading."""
        with self._lock:
            if self.setup_manager is not None:
                self.setup_manager.cancel()
            elif self.kronos is not None:
                self.kronos.close()
            return self.status()

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
        self_test = base_view.get("last_self_test")
        if not isinstance(self_test, dict) or self_test.get("status") != "PASSED":
            return (
                "NOT_VERIFIED",
                "Artifacts and CPU runtime match SHA-256 pins, but offline genuine CPU self-test has not yet run for this interpreter.",
            )
        # Check if latest cached forecast is stale
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
            self._active_inferences += 1

        ikey = f"{mkt}:{sym}"
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
                self.kronos.close()
            with self._lock:
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
                    "message": f"Kronos inference failed closed ({type(exc).__name__}): {str(exc)[:180]}",
                    "at": _utc_now(self._clock).isoformat(),
                }
            raise KronosUnavailableError(self._last_error["message"]) from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            with self._lock:
                self._active_inferences = max(0, self._active_inferences - 1)

        last_close = float(validated_bars[-1].close)
        if not all(
            math.isfinite(v)
            for v in (forecast.mean_close, forecast.q10_close, forecast.q90_close, forecast.up_probability)
        ):
            with self._lock:
                self._last_error = {"code": "INVALID_OUTPUT", "message": "Non-finite Kronos forecast rejected"}
            raise KronosUnavailableError("Non-finite Kronos forecast rejected")
        if forecast.mean_close <= 0 or forecast.q10_close > forecast.q90_close or not (0.0 <= forecast.up_probability <= 1.0):
            with self._lock:
                self._last_error = {"code": "INVALID_OUTPUT", "message": "Malformed quantile/probability bounds rejected"}
            raise KronosUnavailableError("Malformed Kronos quantile/probability bounds rejected")

        last_inf = (self.kronos.status().get("last_inference") or {}) if self.kronos else {}
        load_ms = float(last_inf.get("load_ms") or 0.0)
        infer_ms = float(last_inf.get("inference_ms") or 0.0)
        now = _utc_now(self._clock)
        expected_return_pct = ((forecast.mean_close / last_close) - 1.0) * 100.0
        band_pct = ((forecast.q90_close - forecast.q10_close) / last_close) * 100.0
        directional_conf = abs(forecast.up_probability - 0.5) * 2.0

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
            "last_close": round(last_close, 4),
            "generated_at": now.isoformat(),
            "horizon_bars": use_horizon,
            "samples": use_samples,
            "mean_close": round(forecast.mean_close, 4),
            "q10_close": round(forecast.q10_close, 4),
            "q90_close": round(forecast.q90_close, 4),
            "expected_return_pct": round(expected_return_pct, 4),
            "uncertainty_band_q10_q90_pct": round(band_pct, 4),
            "up_probability": round(forecast.up_probability, 4),
            "directional_dispersion_score": round(directional_conf, 4),
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
            },
            "freshness_state": "FRESH",
        }
        with self._lock:
            self._last_error = None
            if load_ms > 0:
                self._load_ms_samples.append(load_ms)
            if infer_ms > 0:
                self._infer_ms_samples.append(infer_ms)
            self._forecasts[f"{mkt}:{sym}:{tf_norm}"] = record
        return record

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

            return {
                "provider_state": p_state,
                "provider_states_supported": list(KRONOS_PROVIDER_STATES),
                "summary": summary,
                "inference_enabled": self.config.inference_enabled,
                "model_loaded": bool(self.config.model_loaded_preference and p_state == "READY"),
                "execution_authorization_effect": "NONE — forecasts never create, arm, or activate trading",
                "fine_tuning_policy": "DISABLED — Kronos weights are frozen SHA-256-verified foundation artifacts; AXIOM trains separate decision models and strategy parameters.",
                "license": "MIT License (Copyright (c) 2025 ShiYu — shiyu-coder/Kronos)",
                "config": asdict(self.config),
                "diagnostics": {
                    "load_ms_p50": _percentile(loads_sorted, 50.0),
                    "load_ms_p95": _percentile(loads_sorted, 95.0),
                    "inference_ms_p50": _percentile(infers_sorted, 50.0),
                    "inference_ms_p95": _percentile(infers_sorted, 95.0),
                    "inference_count": len(infers_sorted),
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
