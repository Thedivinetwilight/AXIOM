"""Build the AXIOM Kronos stack from environment configuration.

Kronos is OFF unless ``AXIOM_KRONOS_ENABLED=true``. A misconfigured stack never
raises into the GUI: ``build_forecast_service`` returns ``None`` plus a status
the UI can display, so AXIOM keeps working with Kronos disabled or unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time

from axiom.forecast.pipeline import ForecastPipeline
from axiom.forecast.store import ForecastStore
from axiom.kronos.model_cache import ModelCache, default_cache_root
from axiom.market.calendar import (
    ContinuousCalendar,
    DailyCalendar,
    SessionCalendar,
    TradingCalendar,
)
from axiom.market.http import HttpJsonMarketDataProvider
from axiom.market.provider import MarketDataError, MarketDataProvider
from axiom.market.replay import ReplayMarketDataProvider
from axiom.models import KronosConfig, KronosState, KronosStatus, utcnow
from axiom.service import ForecastService
from axiom.worker.manager import KronosManager

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off", ""}


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(env.get(name, "")).strip().lower()
    if raw in _BOOL_FALSE and name not in env:
        return default
    if raw in _BOOL_TRUE:
        return True
    if raw in _BOOL_FALSE:
        return False
    raise ValueError(f"{name} must be true or false (got {raw!r})")


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(str(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from exc


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(str(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number (got {raw!r})") from exc


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name)
    return default if raw in (None, "") else str(raw)


def kronos_config_from_env(env: Mapping[str, str] | None = None) -> KronosConfig:
    """Parse Kronos settings. Defaults match Kronos-small + Tokenizer-base."""
    env = os.environ if env is None else env
    return KronosConfig(
        enabled=_bool(env, "AXIOM_KRONOS_ENABLED", False),
        backend=_text(env, "AXIOM_KRONOS_BACKEND", "upstream"),
        model_id=_text(env, "AXIOM_KRONOS_MODEL", "NeoQuasar/Kronos-small"),
        model_revision=_text(env, "AXIOM_KRONOS_MODEL_REVISION", ""),
        tokenizer_id=_text(env, "AXIOM_KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base"),
        tokenizer_revision=_text(env, "AXIOM_KRONOS_TOKENIZER_REVISION", ""),
        lookback=_int(env, "AXIOM_KRONOS_LOOKBACK", 400),
        pred_len=_int(env, "AXIOM_KRONOS_PRED_LEN", 120),
        max_context=_int(env, "AXIOM_KRONOS_MAX_CONTEXT", 512),
        temperature=_float(env, "AXIOM_KRONOS_TEMPERATURE", 1.0),
        top_k=_int(env, "AXIOM_KRONOS_TOP_K", 0),
        top_p=_float(env, "AXIOM_KRONOS_TOP_P", 0.9),
        sample_paths=_int(env, "AXIOM_KRONOS_SAMPLE_PATHS", 1),
        device=_text(env, "AXIOM_KRONOS_DEVICE", "auto"),
        update_on_closed_bar=_bool(env, "AXIOM_KRONOS_UPDATE_ON_CLOSED_BAR", True),
        min_history=_int(env, "AXIOM_KRONOS_MIN_HISTORY", 64),
        cache_dir=_text(env, "AXIOM_MODEL_CACHE", ""),
        allow_download=_bool(env, "AXIOM_KRONOS_ALLOW_DOWNLOAD", False),
        stale_after_seconds=_float(env, "AXIOM_KRONOS_STALE_AFTER", 3600.0),
    ).validate()


def calendar_from_env(env: Mapping[str, str] | None = None) -> TradingCalendar:
    env = os.environ if env is None else env
    kind = _text(env, "AXIOM_MARKET_CALENDAR", "continuous").lower()
    if kind == "continuous":
        return ContinuousCalendar()
    if kind == "daily":
        weekdays = _weekdays(_text(env, "AXIOM_MARKET_WEEKDAYS", "0-4"))
        return DailyCalendar(
            _text(env, "AXIOM_MARKET_CALENDAR_NAME", "daily"), weekdays=weekdays
        )
    if kind == "session":
        open_time = _clock(_text(env, "AXIOM_MARKET_SESSION_OPEN", "09:15"))
        close_time = _clock(_text(env, "AXIOM_MARKET_SESSION_CLOSE", "15:30"))
        return SessionCalendar(
            _text(env, "AXIOM_MARKET_CALENDAR_NAME", "session"),
            open_time=open_time,
            close_time=close_time,
            weekdays=_weekdays(_text(env, "AXIOM_MARKET_WEEKDAYS", "0-4")),
        )
    raise ValueError(
        f"AXIOM_MARKET_CALENDAR must be continuous, daily or session (got {kind!r})"
    )


def _weekdays(spec: str) -> frozenset[int]:
    days: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            days.update(range(int(start), int(end) + 1))
        else:
            days.add(int(part))
    return frozenset(days)


def _clock(spec: str) -> time:
    hour, _, minute = spec.partition(":")
    return time(int(hour), int(minute or 0))


def provider_from_env(env: Mapping[str, str] | None = None) -> MarketDataProvider:
    """Replay by default (deterministic, offline); HTTP JSON for real data."""
    env = os.environ if env is None else env
    kind = _text(env, "AXIOM_MARKET_DATA_SOURCE", "replay").lower()
    timeframe = _text(env, "AXIOM_MARKET_DATA_TIMEFRAME", "")
    if kind == "replay":
        path = _text(env, "AXIOM_MARKET_DATA_CSV", "")
        if not path:
            raise MarketDataError(
                "AXIOM_MARKET_DATA_CSV must point at a recorded OHLCV CSV "
                "(replay never invents data)"
            )
        if not timeframe:
            raise MarketDataError(
                "AXIOM_MARKET_DATA_TIMEFRAME must declare the CSV timeframe"
            )
        return ReplayMarketDataProvider.from_csv(path, timeframe)
    if kind == "http":
        template = _text(env, "AXIOM_MARKET_DATA_URL", "")
        if not template:
            raise MarketDataError("AXIOM_MARKET_DATA_URL must be set for source=http")
        headers = {}
        token = _text(env, "AXIOM_MARKET_DATA_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return HttpJsonMarketDataProvider(
            template,
            timeframe=timeframe or None,
            headers=headers,
            timeout=_float(env, "AXIOM_MARKET_DATA_TIMEOUT", 20.0),
            candles_path=_text(env, "AXIOM_MARKET_DATA_JSON_PATH", "") or None,
        )
    raise MarketDataError(f"unknown AXIOM_MARKET_DATA_SOURCE: {kind!r}")


def series_from_env(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """``AXIOM_FORECAST_SERIES="DEMO:1h,BTCUSDT:5m"``"""
    env = os.environ if env is None else env
    raw = _text(env, "AXIOM_FORECAST_SERIES", "")
    series: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(
                f"AXIOM_FORECAST_SERIES entries must be INSTRUMENT:TIMEFRAME (got {part!r})"
            )
        instrument, _, timeframe = part.partition(":")
        series.append((instrument.strip(), timeframe.strip()))
    return series


@dataclass(frozen=True)
class KronosEnvironment:
    """Everything the UI needs, or an explicit reason it is unavailable."""

    config: KronosConfig
    service: ForecastService | None
    status: KronosStatus
    series: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.service is not None

    def describe(self) -> dict:
        return {
            "available": self.available,
            "config": self.config.to_dict(),
            "status": self.status.to_dict(),
            "series": [{"instrument": i, "timeframe": t} for i, t in self.series],
            "notes": list(self.notes),
        }


def build_forecast_service(
    env: Mapping[str, str] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    provider: MarketDataProvider | None = None,
    manager: KronosManager | None = None,
    store: ForecastStore | None = None,
) -> KronosEnvironment:
    """Assemble provider + calendar + Kronos worker + pipeline + service."""
    env = os.environ if env is None else env
    clock = clock or utcnow
    notes: list[str] = []
    try:
        config = kronos_config_from_env(env)
    except ValueError as exc:
        return KronosEnvironment(
            KronosConfig(),
            None,
            KronosStatus.error(f"invalid Kronos configuration: {exc}"),
            notes=(str(exc),),
        )
    if not config.enabled:
        return KronosEnvironment(
            config,
            None,
            KronosStatus.disabled("AXIOM_KRONOS_ENABLED is not true"),
            notes=("Kronos is disabled; AXIOM runs without forecasting",),
        )

    try:
        market_provider = provider or provider_from_env(env)
        calendar = calendar_from_env(env)
        series = series_from_env(env)
    except (MarketDataError, ValueError) as exc:
        return KronosEnvironment(
            config,
            None,
            KronosStatus(
                state=KronosState.DATA_ERROR,
                message=f"market data source unavailable: {exc}",
                backend=config.backend,
                model_id=config.model_id,
                updated_at=clock(),
            ),
            notes=(str(exc),),
        )

    mode = _text(env, "AXIOM_KRONOS_MODE", "process").lower()
    if mode not in {"process", "in-process"}:
        mode = "process"
        notes.append("AXIOM_KRONOS_MODE invalid; using 'process'")
    cache = ModelCache(config.cache_dir or None)
    worker = manager or KronosManager(config, mode=mode, clock=clock)

    def infer(bars, future_timestamps):
        return worker.forecast(bars, future_timestamps)

    def model_identity() -> tuple[str, str]:
        """Non-blocking: reads the cached child status, never performs IPC."""
        status = worker.cached_status()
        return (status.model_id, status.model_revision)

    pipeline = ForecastPipeline(
        market_provider,
        config,
        calendar=calendar,
        store=store or ForecastStore(),
        infer=infer,
        model_identity=model_identity,
        clock=clock,
    )
    service = ForecastService(pipeline, manager=worker, clock=clock)
    notes.append(f"model cache: {cache.root}")
    if not series:
        notes.append("AXIOM_FORECAST_SERIES is empty: nothing is scheduled for forecasting")
    return KronosEnvironment(
        config=config,
        service=service,
        status=KronosStatus(
            state=KronosState.INITIALIZING,
            message=f"Kronos {mode} runtime configured (not loaded yet)",
            backend=config.backend,
            model_id=config.model_id,
            model_revision=config.model_revision,
            tokenizer_id=config.tokenizer_id,
            tokenizer_revision=config.tokenizer_revision,
            cache_dir=str(cache.root),
            updated_at=clock(),
        ),
        series=tuple(series),
        notes=tuple(notes),
    )


def model_cache_from_env(env: Mapping[str, str] | None = None) -> ModelCache:
    env = os.environ if env is None else env
    root = _text(env, "AXIOM_MODEL_CACHE", "")
    return ModelCache(root or None) if root else ModelCache(default_cache_root())
