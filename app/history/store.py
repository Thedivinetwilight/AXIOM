"""Local OHLCV storage: partitioned gzip CSV (default) or Parquet when available.

Layout: {root}/{instrument_slug}/{unit}-{interval}/{YYYY-MM}.csv.gz
- Each partition holds one calendar month (UTC) of candles, ordered by ts.
- Deduplication is timestamp-based: rewriting a partition with overlapping
  data never duplicates candles (existing timestamps win; new timestamps
  append). This is what makes resumed downloads idempotent.
- Parquet is used only if pyarrow is importable AND enabled; format recorded
  per dataset. Source data is never edited in place — partitions are
  rewritten atomically (tmp file + os.replace).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

COLUMNS = ["ts", "open", "high", "low", "close", "volume", "oi"]


def _slug(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)


def parse_ts(value) -> datetime:
    """Instant (UTC) for comparisons/arithmetics."""
    return parse_local(value).astimezone(timezone.utc)


def parse_local(value) -> datetime:
    """Original wall time with its source offset (Upstox sends +05:30).

    Exchange-local dates drive partitioning and date-range filtering: a daily
    candle stamped 2025-06-01T00:00+05:30 belongs to June 1, not May 31 UTC.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def month_of(ts: datetime) -> str:
    return f"{ts.year:04d}-{ts.month:02d}"


class CandleStore:
    def __init__(self, root: str | Path, *, parquet_enabled: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._parquet = parquet_enabled and self._parquet_available()

    @staticmethod
    def _parquet_available() -> bool:
        try:
            import pyarrow  # noqa: F401

            return True
        except ImportError:
            return False

    @property
    def format(self) -> str:
        return "parquet" if self._parquet else "csv.gz"

    def partition_path(self, instrument_key: str, unit: str, interval: int,
                       month: str) -> Path:
        return (self.root / _slug(instrument_key) / f"{unit}-{interval}"
                / f"{month}.{self.format}")

    def _read_partition(self, path: Path) -> dict[str, list]:
        if not path.exists():
            return {}
        if path.suffix == ".parquet":
            import pyarrow.parquet as pq

            table = pq.read_table(path).to_pydict()
            return {str(ts): row for ts, row in
                    zip(table["ts"], zip(*[table[c] for c in COLUMNS[1:]]))}
        rows = {}
        with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows[row["ts"]] = [row[c] for c in COLUMNS[1:]]
        return rows

    def _write_partition(self, path: Path, rows: dict[str, list]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        if path.suffix == ".parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq

            columns = {c: [] for c in COLUMNS}
            for ts in sorted(rows):
                columns["ts"].append(ts)
                for col, value in zip(COLUMNS[1:], rows[ts]):
                    columns[col].append(value)
            pq.write_table(pa.table(columns), tmp)
        else:
            with gzip.open(tmp, "wt", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(COLUMNS)
                for ts in sorted(rows):
                    writer.writerow([ts, *rows[ts]])
        os.replace(tmp, path)

    def upsert_candles(self, instrument_key: str, unit: str, interval: int,
                       candles: list[dict]) -> dict:
        """candles: [{ts, open, high, low, close, volume, oi}] (ts ISO-8601 UTC).

        Returns per-partition statistics including how many rows were new.
        """
        stats = {"received": len(candles), "new": 0, "partitions": {}}
        by_month: dict[str, dict[str, list]] = {}
        for candle in candles:
            ts = str(candle["ts"])
            parse_ts(ts)  # validation only
            by_month.setdefault(month_of(parse_local(ts)), {})[ts] = [
                candle.get("open"), candle.get("high"), candle.get("low"),
                candle.get("close"), candle.get("volume", 0),
                candle.get("oi", 0),
            ]
        for month, rows in sorted(by_month.items()):
            path = self.partition_path(instrument_key, unit, interval, month)
            existing = self._read_partition(path)
            fresh = {ts: row for ts, row in rows.items() if ts not in existing}
            merged = {**existing, **rows}
            self._write_partition(path, merged)
            stats["new"] += len(fresh)
            stats["partitions"][month] = {
                "new": len(fresh), "total": len(merged), "path": str(path),
            }
        return stats

    def read_range(self, instrument_key: str, unit: str, interval: int,
                   from_ts: str | None = None, to_ts: str | None = None) -> list[dict]:
        """Chronological candles in [from_ts, to_ts] (ISO strings, inclusive)."""

        directory = (self.root / _slug(instrument_key) / f"{unit}-{interval}")
        if not directory.exists():
            return []
        # Date-only bounds ("2025-06-01") are compared by calendar date so that
        # IST-midnight daily candles are not cut by UTC midnight arithmetic.
        if from_ts and len(from_ts) == 10:
            start_date = date.fromisoformat(from_ts)
        else:
            start_date = None
        if to_ts and len(to_ts) == 10:
            end_date = date.fromisoformat(to_ts)
        else:
            end_date = None
        start = parse_ts(from_ts) if from_ts and start_date is None else None
        end = parse_ts(to_ts) if to_ts and end_date is None else None
        out: list[dict] = []
        for path in sorted(directory.iterdir()):
            for ts, row in self._read_partition(path).items():
                ts_dt = parse_ts(ts)
                local = parse_local(ts)
                if start_date and local.date() < start_date:
                    continue
                if end_date and local.date() > end_date:
                    continue
                if start and ts_dt < start:
                    continue
                if end and ts_dt > end:
                    continue
                out.append({"ts": ts, "open": float(row[0]), "high": float(row[1]),
                            "low": float(row[2]), "close": float(row[3]),
                            "volume": float(row[4]), "oi": float(row[5])})
        out.sort(key=lambda item: parse_ts(item["ts"]))
        return out

    def to_candles(self, rows: list[dict], symbol: str):
        from app.data.models import Candle

        return [Candle(parse_ts(r["ts"]), r["open"], r["high"], r["low"],
                       r["close"], r["volume"], symbol) for r in rows]

    def file_hash(self, path: str | Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def store_fingerprint(self, instrument_key: str, unit: str, interval: int) -> dict:
        """Hashes of every partition for a series (dataset audit support)."""
        directory = self.root / _slug(instrument_key) / f"{unit}-{interval}"
        if not directory.exists():
            return {}
        return {path.name: self.file_hash(path) for path in sorted(directory.iterdir())}
