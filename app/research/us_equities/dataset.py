"""Strict, point-in-time US equity daily-bar dataset contract.

The harness will only evaluate a dataset that declares — and can prove — where
its prices came from. This module never downloads data, never interpolates a
missing bar, and never adjusts a price: it either accepts a dataset that
satisfies the contract or refuses with a list of exactly what is missing.

Required manifest (JSON)::

    {
      "schema": "axiom.us.equities.dataset/1",
      "source": {
        "name": "Alpaca Markets historical bars",
        "license": "Alpaca Market Data terms, <plan>",
        "url": "https://alpaca.markets/data",
        "retrieved_at": "2026-10-01T00:00:00Z",
        "feed": "sip"
      },
      "adjustment": "split_dividend",       // raw | split | split_dividend
      "price_basis": "total_return_prices", // total_return_prices | price_only
      "bar_timeframe": "1D",
      "bar_timestamp_convention": "session_close",
      "timezone": "UTC",
      "benchmark": "SPY",
      "universe": {
        "kind": "point_in_time",            // point_in_time | static_survivors
        "membership_csv": "universe.csv",   // symbol,start,end
        "description": "..."
      },
      "bars_csv": "bars.csv",               // symbol,timestamp,open,high,low,close,volume
      "corporate_actions_csv": "corporate_actions.csv"  // optional, documented below
    }

Why each field is load-bearing:

``adjustment`` / ``price_basis``
    Split-only adjustment leaves total-return comparisons wrong; raw prices make
    a 2:1 split look like a -50% day. The harness reports the policy verbatim
    and refuses ``raw`` without a corporate-actions file.

``bar_timestamp_convention``
    A daily bar stamped at the *open* and used as if it were the close is
    look-ahead. Only ``session_close`` is accepted, and every timestamp is
    verified to sit within five minutes of the real New York close for that
    date (early closes included).

``universe.kind``
    ``static_survivors`` is allowed for exploration but is explicitly recorded
    as survivorship-biased and can never produce a strategy-selection verdict.

``corporate_actions_csv``
    Columns ``symbol,ex_date,type,ratio,amount`` where ``type`` is ``split`` or
    ``dividend``. Present so an operator can audit that the declared adjustment
    policy is actually supported by the vendor files that were delivered.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path

from app.data.models import Candle, parse_timestamp
from app.markets.us_calendar import USEquitiesCalendar

SCHEMA = "axiom.us.equities.dataset/1"
REQUIRED_TOP = ("schema", "source", "adjustment", "bar_timeframe",
                "bar_timestamp_convention", "timezone", "universe", "bars_csv")
ALLOWED_ADJUSTMENT = {"raw", "split", "split_dividend"}
ALLOWED_BASIS = {"total_return_prices", "price_only"}
CLOSE_TOLERANCE_SECONDS = 300


class DatasetError(ValueError):
    """Raised with a human-readable list of contract violations."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("US equity dataset rejected: " + "; ".join(self.problems[:12]))


@dataclass(frozen=True)
class MembershipSpan:
    symbol: str
    start: date
    end: date

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass
class USEquityDataset:
    root: Path
    manifest: dict
    bars: dict[str, list[Candle]]
    membership: dict[str, list[MembershipSpan]]
    benchmark: str | None
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ facts
    @property
    def source(self) -> dict:
        return self.manifest["source"]

    @property
    def adjustment(self) -> str:
        return self.manifest["adjustment"]

    @property
    def price_basis(self) -> str:
        return self.manifest.get("price_basis", "price_only")

    @property
    def universe_kind(self) -> str:
        return self.manifest["universe"]["kind"]

    @property
    def survivorship_biased(self) -> bool:
        return self.universe_kind != "point_in_time"

    @property
    def symbols(self) -> list[str]:
        return sorted(self.bars)

    @property
    def dates(self) -> list[date]:
        days = {bar.timestamp.astimezone(timezone.utc).date()
                for rows in self.bars.values() for bar in rows}
        return sorted(days)

    def fingerprint(self) -> str:
        """Stable content hash of the manifest, so a report names its dataset."""
        payload = json.dumps(self.manifest, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def members(self, day: date) -> set[str]:
        """Point-in-time universe membership for ``day`` (never a survivor list)."""
        if self.survivorship_biased:
            return {symbol for symbol in self.bars if self.bars[symbol]}
        return {
            symbol for symbol, spans in self.membership.items()
            if any(span.covers(day) for span in spans) and symbol in self.bars
        }

    def bars_for(self, day: date) -> dict[str, Candle]:
        """Bars stamped for ``day`` (session close of that New York date)."""
        return {
            symbol: rows_by_day[day]
            for symbol, rows_by_day in self._indexed().items()
            if day in rows_by_day
        }

    def _indexed(self) -> dict[str, dict[date, Candle]]:
        cached = getattr(self, "_index_cache", None)
        if cached is None:
            cached = {
                symbol: {
                    bar.timestamp.astimezone(timezone.utc).date(): bar
                    for bar in rows
                }
                for symbol, rows in self.bars.items()
            }
            self._index_cache = cached
        return cached

    def benchmark_bars(self) -> list[Candle]:
        return list(self.bars.get(self.benchmark or "", []))


def _read_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.is_file():
        raise DatasetError([f"manifest.json is missing at {path}"])
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError([f"manifest.json is not valid JSON: {type(exc).__name__}"]) from exc
    if not isinstance(manifest, dict):
        raise DatasetError(["manifest.json must contain a JSON object"])
    return manifest


def _validate_manifest(manifest: dict, root: Path) -> list[str]:
    problems = []
    if manifest.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}, found {manifest.get('schema')!r}")
    for key in REQUIRED_TOP:
        if key not in manifest:
            problems.append(f"missing required manifest field {key!r}")
    source = manifest.get("source")
    if not isinstance(source, dict):
        problems.append("source must be an object with name/license/url/retrieved_at")
    else:
        for key in ("name", "license", "url", "retrieved_at"):
            if not str(source.get(key, "")).strip():
                problems.append(f"source.{key} is required: an unattributed dataset is not evidence")
        try:
            parse_timestamp(source.get("retrieved_at", ""))
        except (ValueError, TypeError):
            problems.append("source.retrieved_at must be an ISO-8601 timestamp with timezone")
    if manifest.get("adjustment") not in ALLOWED_ADJUSTMENT:
        problems.append(f"adjustment must be one of {sorted(ALLOWED_ADJUSTMENT)}")
    if manifest.get("price_basis", "price_only") not in ALLOWED_BASIS:
        problems.append(f"price_basis must be one of {sorted(ALLOWED_BASIS)}")
    if manifest.get("bar_timeframe") != "1D":
        problems.append("bar_timeframe must be '1D' (this harness is daily-bar only)")
    if manifest.get("bar_timestamp_convention") != "session_close":
        problems.append(
            "bar_timestamp_convention must be 'session_close'; an open-stamped daily "
            "bar used as a close is look-ahead"
        )
    if str(manifest.get("timezone", "")).upper() not in {"UTC", "ET", "AMERICA/NEW_YORK"}:
        problems.append("timezone must be UTC (bars are stored in UTC)")
    universe = manifest.get("universe")
    if not isinstance(universe, dict):
        problems.append("universe must be an object with kind/membership_csv/description")
    else:
        if universe.get("kind") not in {"point_in_time", "static_survivors"}:
            problems.append("universe.kind must be point_in_time or static_survivors")
        if universe.get("kind") == "point_in_time":
            membership = universe.get("membership_csv")
            if not membership or not (root / membership).is_file():
                problems.append(
                    "universe.kind=point_in_time requires an existing membership_csv "
                    "(symbol,start,end); a fixed list cannot represent a point-in-time universe"
                )
        if not str(universe.get("description", "")).strip():
            problems.append("universe.description must state how the universe was constructed")
    bars_csv = manifest.get("bars_csv")
    if not bars_csv or not (root / bars_csv).is_file():
        problems.append(f"bars_csv not found: {bars_csv!r}")
    actions = manifest.get("corporate_actions_csv")
    if actions and not (root / actions).is_file():
        problems.append(f"corporate_actions_csv declared but not found: {actions!r}")
    if manifest.get("adjustment") == "raw" and not actions:
        problems.append(
            "adjustment=raw requires corporate_actions_csv: without it, splits and "
            "dividends silently corrupt every return"
        )
    return problems


def _load_bars(path: Path, calendar: USEquitiesCalendar) -> tuple[dict[str, list[Candle]], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    rows_by_symbol: dict[str, list[Candle]] = {}
    seen: set[tuple[str, datetime]] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"symbol", "timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise DatasetError([f"bars_csv is missing columns: {', '.join(sorted(missing))}"])
        for index, row in enumerate(reader, start=2):
            try:
                symbol = str(row["symbol"]).strip().upper()
                if not symbol:
                    raise ValueError("empty symbol")
                timestamp = parse_timestamp(row["timestamp"])
                values = [float(row[key]) for key in ("open", "high", "low", "close", "volume")]
            except (ValueError, TypeError) as exc:
                problems.append(f"bars_csv row {index}: malformed value ({exc})")
                continue
            open_, high, low, close, volume = values
            if not all(isfinite(v) for v in values) or min(open_, high, low, close) <= 0:
                problems.append(f"bars_csv row {index}: non-finite or non-positive price")
                continue
            if volume < 0 or low > min(open_, close) or high < max(open_, close) or low > high:
                problems.append(f"bars_csv row {index}: impossible OHLC/volume")
                continue
            key = (symbol, timestamp)
            if key in seen:
                problems.append(f"bars_csv row {index}: duplicate bar for {symbol} at {timestamp}")
                continue
            seen.add(key)
            rows_by_symbol.setdefault(symbol, []).append(
                Candle(timestamp, open_, high, low, close, volume, symbol)
            )
    if problems:
        raise DatasetError(problems)
    if not rows_by_symbol:
        raise DatasetError(["bars_csv contains no rows"])

    for symbol, rows in rows_by_symbol.items():
        rows.sort(key=lambda bar: bar.timestamp)
        for previous, current in zip(rows, rows[1:]):
            if current.timestamp <= previous.timestamp:
                problems.append(f"{symbol}: bars are not strictly increasing")
        # Session-close verification: the whole no-look-ahead claim rests on it.
        for bar in rows:
            local = bar.timestamp.astimezone(calendar_zone())
            day = local.date()
            bounds = calendar.session_bounds(day)
            if bounds is None:
                problems.append(
                    f"{symbol}: bar at {bar.timestamp.isoformat()} falls on a non-trading day"
                )
                continue
            close = bounds[1]
            if abs((local - close).total_seconds()) > CLOSE_TOLERANCE_SECONDS:
                problems.append(
                    f"{symbol}: bar at {bar.timestamp.isoformat()} is stamped "
                    f"{local.time().isoformat()} New York, but the {day.isoformat()} "
                    f"session closes at {close.time().isoformat()}"
                )
        if len(rows) < 2:
            warnings.append(f"{symbol}: fewer than two bars, excluded from evaluation")
    if problems:
        raise DatasetError(problems)
    return rows_by_symbol, warnings


def calendar_zone():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def _load_membership(path: Path) -> dict[str, list[MembershipSpan]]:
    problems = []
    spans: dict[str, list[MembershipSpan]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = {"symbol", "start", "end"} - set(reader.fieldnames or [])
        if missing:
            raise DatasetError(
                [f"membership_csv is missing columns: {', '.join(sorted(missing))}"]
            )
        for index, row in enumerate(reader, start=2):
            try:
                symbol = str(row["symbol"]).strip().upper()
                start = date.fromisoformat(str(row["start"]).strip())
                end = date.fromisoformat(str(row["end"]).strip())
            except (ValueError, TypeError) as exc:
                problems.append(f"membership_csv row {index}: {exc}")
                continue
            if not symbol:
                problems.append(f"membership_csv row {index}: empty symbol")
                continue
            if end < start:
                problems.append(f"membership_csv row {index}: end before start for {symbol}")
                continue
            spans.setdefault(symbol, []).append(MembershipSpan(symbol, start, end))
    if problems:
        raise DatasetError(problems)
    if not spans:
        raise DatasetError(["membership_csv contains no rows"])
    return spans


def _load_corporate_actions(path: Path) -> list[dict]:
    problems, actions = [], []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = {"symbol", "ex_date", "type"} - set(reader.fieldnames or [])
        if missing:
            raise DatasetError(
                [f"corporate_actions_csv is missing columns: {', '.join(sorted(missing))}"]
            )
        for index, row in enumerate(reader, start=2):
            try:
                symbol = str(row["symbol"]).strip().upper()
                ex_date = date.fromisoformat(str(row["ex_date"]).strip())
                kind = str(row["type"]).strip().lower()
            except (ValueError, TypeError) as exc:
                problems.append(f"corporate_actions_csv row {index}: {exc}")
                continue
            if kind not in {"split", "dividend"}:
                problems.append(f"corporate_actions_csv row {index}: unknown type {kind!r}")
                continue
            if not symbol:
                problems.append(f"corporate_actions_csv row {index}: empty symbol")
                continue
            actions.append({"symbol": symbol, "ex_date": ex_date, "type": kind,
                            "ratio": row.get("ratio") or None,
                            "amount": row.get("amount") or None})
    if problems:
        raise DatasetError(problems)
    return actions


def load_dataset(root: str | Path) -> USEquityDataset:
    """Load and fully validate a dataset directory. Refuses rather than repairs."""
    root = Path(root)
    if not root.is_dir():
        raise DatasetError([f"dataset directory does not exist: {root}"])
    manifest = _read_manifest(root)
    problems = _validate_manifest(manifest, root)
    if problems:
        raise DatasetError(problems)
    calendar = USEquitiesCalendar()
    bars, warnings = _load_bars(root / manifest["bars_csv"], calendar)
    membership: dict[str, list[MembershipSpan]] = {}
    if manifest["universe"]["kind"] == "point_in_time":
        membership = _load_membership(root / manifest["universe"]["membership_csv"])
        missing_bars = sorted(set(membership) - set(bars))
        if missing_bars:
            problems.append(
                "membership lists symbols with no bars: " + ", ".join(missing_bars[:10])
            )
    if manifest.get("corporate_actions_csv"):
        actions = _load_corporate_actions(root / manifest["corporate_actions_csv"])
        if manifest["adjustment"] != "raw" and not actions:
            warnings.append(
                "adjustment declares corporate actions but the actions file is empty; "
                "verify with the vendor that adjusted bars were used"
            )
    benchmark = manifest.get("benchmark")
    if benchmark and benchmark not in bars:
        problems.append(
            f"benchmark {benchmark!r} has no bars; regime filters and buy-and-hold "
            "comparison need it"
        )
    if problems:
        raise DatasetError(problems)
    dataset = USEquityDataset(
        root=root, manifest=manifest, bars=bars, membership=membership,
        benchmark=benchmark, warnings=warnings,
    )
    if dataset.survivorship_biased:
        dataset.warnings.append(
            "universe.kind=static_survivors: results are survivorship-biased and no "
            "strategy-selection verdict can be issued from this dataset"
        )
    return dataset


def describe_requirements() -> dict:
    """The exact contract, for the CLI's "what data is missing" report."""
    return {
        "schema": SCHEMA,
        "required_manifest_fields": list(REQUIRED_TOP),
        "bars_csv_columns": ["symbol", "timestamp", "open", "high", "low", "close", "volume"],
        "membership_csv_columns": ["symbol", "start", "end"],
        "corporate_actions_csv_columns": ["symbol", "ex_date", "type", "ratio", "amount"],
        "bar_timeframe": "1D",
        "bar_timestamp_convention": "session_close",
        "notes": [
            "timestamps must be timezone-aware and stored in UTC",
            "each bar must sit within 5 minutes of its New York session close",
            "raw prices require a corporate-actions file",
            "point-in-time membership requires an entry/exit date per symbol",
        ],
    }
