"""Where chart candles come from, stated explicitly.

Three sources exist, in priority order, and the payload always names the one it
used so a chart can never be mistaken for something it is not:

1. ``engine``   — bars the running (or last running) paper engine actually
                  processed. This is what the strategy saw.
2. ``dataset``  — a CSV in the configured data directory that contains the
                  symbol, for previewing before a session starts. This is the
                  same file the replay source will feed the engine.
3. ``none``     — nothing. The series is empty and the payload says why.

Nothing is interpolated, forward-filled or generated.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from app.charting.series import _seconds, can_aggregate, resample
from app.data.models import Candle, TIMEFRAMES

MAX_DATASET_BYTES = 5_000_000
_CACHE_LIMIT = 8


@dataclass(frozen=True)
class ResolvedSeries:
    candles: list[Candle] = field(default_factory=list)
    source: str = "none"
    source_label: str = "no data"
    source_timeframe: str = ""
    timeframe: str = ""
    live: bool = False
    note: str | None = None
    available_timeframes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.candles


def _dataset_symbols(path: Path) -> set[str]:
    """Symbols present in a CSV, read without validating the whole file."""
    symbols: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(io.StringIO(handle.read(MAX_DATASET_BYTES))):
                symbol = str(row.get("symbol", "")).strip()
                if symbol:
                    symbols.add(symbol)
                if len(symbols) > 64:
                    break
    except (OSError, UnicodeDecodeError, csv.Error):
        return set()
    return symbols


def _declared_timeframe(path: Path, rows_checked: int = 3) -> str | None:
    """Infer a dataset's timeframe from its first rows only.

    CSV files carry no timeframe metadata in this project, so the spacing of
    the first few timestamps is the only evidence available. A file that cannot
    be inferred is skipped rather than guessed at.
    """
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            stamps = []
            for row in reader:
                if len(stamps) >= rows_checked:
                    break
                stamps.append(str(row.get("timestamp", "")))
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    from app.data.models import parse_timestamp

    try:
        moments = sorted({parse_timestamp(s) for s in stamps if s})
    except ValueError:
        return None
    if len(moments) < 2:
        return None
    gaps = {(b - a).total_seconds() for a, b in zip(moments, moments[1:])}
    if len(gaps) != 1:
        return None
    gap = gaps.pop()
    for name, seconds in TIMEFRAMES.items():
        if abs(gap - seconds) < 1e-6:
            return name
    return None


def _cache(app):
    cache = getattr(app.state, "chart_dataset_cache", None)
    if cache is None:
        cache = {}
        app.state.chart_dataset_cache = cache
    return cache


def dataset_index(app, *, limit_files: int = 12) -> dict[str, list[str]]:
    """{symbol: [dataset filenames]} for the configured data directory."""
    root = Path(app.state.data_dir)
    index: dict[str, list[str]] = {}
    if not root.is_dir():
        return index
    for path in sorted(root.glob("*.csv"))[:limit_files]:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_DATASET_BYTES:
                continue
        except OSError:
            continue
        for symbol in _dataset_symbols(path):
            index.setdefault(symbol, []).append(path.name)
    return index


def _load_dataset(app, path: Path, symbol: str, timeframe: str) -> list[Candle] | None:
    """Validated candles for `symbol` at `timeframe` from one CSV, or None."""
    from app.data.historical import CSVProvider

    if _declared_timeframe(path) != timeframe:
        return None
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, symbol, timeframe)
    cache = _cache(app)
    if key in cache:
        return cache[key]
    try:
        bars = CSVProvider(path, timeframe).historical(symbol, timeframe)
    except (ValueError, OSError):
        bars = None
    cache[key] = bars
    while len(cache) > _CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    return bars


def dataset_timeframes(app, symbol: str) -> list[str]:
    """Every timeframe in which `symbol` can be drawn from local datasets."""
    root = Path(app.state.data_dir)
    found: set[str] = set()
    for name in dataset_index(app).get(symbol, []):
        declared = _declared_timeframe(root / name)
        if declared:
            found.add(declared)
    return sorted(found, key=_seconds)


def symbol_timeframes(app, symbol: str, engine_timeframe: str | None = None) -> list[str]:
    """Every timeframe `symbol` can actually be drawn at, finest first."""
    frames: set[str] = set()
    if engine_timeframe and engine_bars(app, symbol):
        frames.update(
            tf for tf in TIMEFRAMES if can_aggregate(engine_timeframe, tf)
        )
    if dataset_timeframes(app, symbol):
        for declared in dataset_timeframes(app, symbol):
            frames.update(tf for tf in TIMEFRAMES if can_aggregate(declared, tf))
    return sorted(frames, key=_seconds)


def declared_timeframe(path: Path) -> str | None:
    """Public name for the timeframe inferred from a dataset's timestamps."""
    return _declared_timeframe(path)


def engine_bars_symbols(app) -> list[str]:
    """Symbols the engine has drawn bars for in this process."""
    simulator = getattr(app.state, "simulator", None)
    series = getattr(simulator, "chart_bars", None) or {}
    return [symbol for symbol, bars in series.items() if bars]


def engine_bars(app, symbol: str) -> list[Candle]:
    """Bars the paper engine has processed this process, newest last."""
    simulator = getattr(app.state, "simulator", None)
    series = getattr(simulator, "chart_bars", None) or {}
    return list(series.get(symbol, []))


def resolve_timeframe(candles: list[Candle], source_timeframe: str, wanted: str) -> list[Candle]:
    if not candles or source_timeframe == wanted:
        return list(candles)
    if not can_aggregate(source_timeframe, wanted):
        raise ValueError(
            f"{wanted} cannot be derived from {source_timeframe}: only exact "
            "intraday multiples are aggregated, and daily bars are never "
            "synthesised from intraday ones"
        )
    return resample(candles, source_timeframe, wanted)


def resolve(app, symbol: str, timeframe: str, limit: int) -> ResolvedSeries:
    """Candles for (symbol, timeframe) with honest provenance, newest last."""
    from app.data.models import validate_symbol

    validate_symbol(symbol)
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")

    simulator = getattr(app.state, "simulator", None)
    broker = getattr(app.state, "broker", None)
    engine_timeframe = getattr(broker, "timeframe", None) or timeframe

    # 1) What the engine processed.
    bars = engine_bars(app, symbol)
    if bars:
        try:
            derived = resolve_timeframe(bars, engine_timeframe, timeframe)
        except ValueError as exc:
            return ResolvedSeries(
                source="engine",
                source_label="paper engine",
                source_timeframe=engine_timeframe,
                timeframe=timeframe,
                live=bool(getattr(simulator, "task", None)),
                note=str(exc),
                available_timeframes=[engine_timeframe],
            )
        return ResolvedSeries(
            candles=derived[-limit:],
            source="engine",
            source_label="paper engine feed",
            source_timeframe=engine_timeframe,
            timeframe=timeframe,
            live=bool(getattr(simulator, "task", None)),
            note=(
                None
                if len(derived) >= limit
                else "showing the bars processed since this process started"
            ),
            available_timeframes=[engine_timeframe],
        )

    # 2) A dataset that contains the symbol. Several files may hold the same
    #    symbol at different timeframes (a 1D file and a 1h file, say), so an
    #    exact match wins and an aggregatable one is the fallback; a file that
    #    cannot serve the request is never used to answer it.
    root = Path(app.state.data_dir)
    candidates = [
        (name, _declared_timeframe(root / name))
        for name in dataset_index(app).get(symbol, [])
    ]
    candidates = [(name, declared) for name, declared in candidates if declared]
    ordered = sorted(
        candidates,
        key=lambda item: (item[1] != timeframe, _seconds(item[1])),
    )
    for name, dataset_timeframe in ordered:
        if not can_aggregate(dataset_timeframe, timeframe):
            continue
        bars = _load_dataset(app, root / name, symbol, dataset_timeframe)
        if not bars:
            continue
        try:
            derived = resolve_timeframe(bars, dataset_timeframe, timeframe)
        except ValueError:
            continue
        note = "preview of a local dataset; no session is running for this symbol"
        if dataset_timeframe != timeframe:
            note += (
                f" · {timeframe} bars aggregated from the {dataset_timeframe} "
                f"bars in {name} (complete buckets only)"
            )
        return ResolvedSeries(
            candles=derived[-limit:],
            source=f"dataset:{name}",
            source_label=f"dataset {name}",
            source_timeframe=dataset_timeframe,
            timeframe=timeframe,
            # A dataset is preview material, never a live feed.
            live=False,
            note=note,
            available_timeframes=dataset_timeframes(app, symbol),
        )

    # 3) Nothing at this timeframe. If a dataset for the symbol exists at some
    #    other timeframe, say that instead of claiming there is no data at all.
    if candidates:
        name, declared = ordered[0]
        return ResolvedSeries(
            source=f"dataset:{name}",
            source_label=f"dataset {name}",
            source_timeframe=declared,
            timeframe=timeframe,
            note=(
                f"{timeframe} cannot be derived from the {declared} bars in "
                f"{name}: only exact intraday multiples are aggregated, and "
                "daily bars are never synthesised from intraday ones"
            ),
            available_timeframes=[d for _, d in candidates],
        )

    # 4) Nothing. Say so.
    last = getattr(broker, "last_candles", {}).get(symbol)
    if last:
        try:
            only = Candle.from_dict(last)
        except (KeyError, TypeError, ValueError):
            only = None
        if only is not None:
            return ResolvedSeries(
                candles=[only],
                source="engine:last-bar",
                source_label="last completed bar",
                source_timeframe=engine_timeframe,
                timeframe=engine_timeframe,
                note="only the last completed bar is available",
                available_timeframes=[engine_timeframe],
            )
    return ResolvedSeries(
        source="none",
        source_label="no data",
        timeframe=timeframe,
        note=(
            "no bars for this symbol yet: start a paper session, or place a CSV "
            "in the data directory"
        ),
        available_timeframes=[],
    )
