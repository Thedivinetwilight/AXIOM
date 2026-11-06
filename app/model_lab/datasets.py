"""Historical Dataset Manager for NSE & NASDAQ research runs (PAPER/RESEARCH ONLY).

Tracks provenance, feed, timezone, corporate-action handling, survivorship-bias
awareness, missing bars, duplicate/out-of-order checks, and SHA-256 dataset
fingerprints. Bundled synthetic fixtures in ``data/sample*.csv`` are explicitly
labeled ``BUNDLED_SYNTHETIC_FIXTURE`` (``real_market_data=False``) and never
represented as real market history.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.data.corporate_actions import (
    AdjustmentPolicy,
    CorporateActionsBook,
    canonical_bars_sha256,
    dataset_block,
)
from app.data.models import TIMEFRAMES, Candle, parse_timestamp

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1D": 86400,
}

BUNDLED_FIXTURES: tuple[dict[str, Any], ...] = (
    {
        "dataset_id": "bundled_nse_synthetic_1h",
        "filename": "sample_1h.csv",
        "market": "NSE",
        "symbol": "DEMO_NSE_SYNTH",
        "timeframe": "1h",
        "timezone": "Asia/Kolkata",
        "source": "Bundled AXIOM Synthetic Fixture (data/sample_1h.csv)",
        "feed": "SYNTHETIC_FIXTURE",
        "corporate_action_adjustment": "NOT_APPLICABLE_SYNTHETIC",
        "survivorship_aware": False,
        "real_market_data": False,
    },
    {
        "dataset_id": "bundled_nasdaq_synthetic_1h",
        "filename": "sample_alt_1h.csv",
        "market": "NASDAQ",
        "symbol": "DEMO_NASDAQ_SYNTH",
        "timeframe": "1h",
        "timezone": "America/New_York",
        "source": "Bundled AXIOM Synthetic Fixture (data/sample_alt_1h.csv)",
        "feed": "SYNTHETIC_FIXTURE",
        "corporate_action_adjustment": "NOT_APPLICABLE_SYNTHETIC",
        "survivorship_aware": False,
        "real_market_data": False,
    },
    {
        "dataset_id": "bundled_nse_synthetic_1d",
        "filename": "sample.csv",
        "market": "NSE",
        "symbol": "DEMO_NSE_DAILY_SYNTH",
        "timeframe": "1D",
        "timezone": "Asia/Kolkata",
        "source": "Bundled AXIOM Synthetic Fixture (data/sample.csv)",
        "feed": "SYNTHETIC_FIXTURE",
        "corporate_action_adjustment": "NOT_APPLICABLE_SYNTHETIC",
        "survivorship_aware": False,
        "real_market_data": False,
    },
    {
        "dataset_id": "bundled_nasdaq_synthetic_1d",
        "filename": "sample_alt.csv",
        "market": "NASDAQ",
        "symbol": "DEMO_NASDAQ_DAILY_SYNTH",
        "timeframe": "1D",
        "timezone": "America/New_York",
        "source": "Bundled AXIOM Synthetic Fixture (data/sample_alt.csv)",
        "feed": "SYNTHETIC_FIXTURE",
        "corporate_action_adjustment": "NOT_APPLICABLE_SYNTHETIC",
        "survivorship_aware": False,
        "real_market_data": False,
    },
)


# ``canonical_bars_sha256`` is re-exported from app.data.corporate_actions so the
# research dataset fingerprint and the corporate-action provenance share one formula.


def inspect_and_clean_bars(
    raw_rows: Iterable[dict[str, Any] | Candle],
    *,
    symbol: str,
    timeframe: str,
) -> tuple[list[Candle], dict[str, Any]]:
    tf_raw = (timeframe or "1D").strip()
    tf_norm = "1D" if tf_raw.lower() == "1d" else tf_raw
    if tf_norm not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")
    step_s = TIMEFRAME_SECONDS.get(tf_norm, 86400)
    clean_sym = (symbol or "UNKNOWN").strip().upper().replace("|", "-")

    bars: list[Candle] = []
    duplicate_timestamps = 0
    out_of_order = 0
    invalid_rows = 0
    seen_ts: set[str] = set()
    prev_dt: datetime | None = None

    for row in raw_rows:
        try:
            if isinstance(row, Candle):
                ts = row.timestamp
                o, h, lo, cl, vol = row.open, row.high, row.low, row.close, float(row.volume)
            else:
                ts = parse_timestamp(str(row["timestamp"]))
                o = float(row["open"])
                h = float(row["high"])
                lo = float(row["low"])
                cl = float(row["close"])
                vol = float(row.get("volume", 0.0))
        except (KeyError, TypeError, ValueError):
            invalid_rows += 1
            continue

        if not all(math.isfinite(v) for v in (o, h, lo, cl, vol)):
            invalid_rows += 1
            continue
        if min(o, h, lo, cl) <= 0 or vol < 0 or h < max(o, cl) or lo > min(o, cl):
            invalid_rows += 1
            continue

        iso = ts.isoformat()
        if iso in seen_ts:
            duplicate_timestamps += 1
            continue
        if prev_dt is not None and ts <= prev_dt:
            out_of_order += 1
            continue

        seen_ts.add(iso)
        prev_dt = ts
        bars.append(
            Candle(
                timestamp=ts,
                open=o,
                high=h,
                low=lo,
                close=cl,
                volume=vol,
                symbol=clean_sym,
            )
        )

    missing_bars_est = 0
    for idx in range(1, len(bars)):
        delta_s = (bars[idx].timestamp - bars[idx - 1].timestamp).total_seconds()
        # Only count intraday/same-week gaps smaller than weekend closures as missing bars
        if step_s < 86400 and step_s * 1.5 < delta_s <= step_s * 12:
            missing_bars_est += round(delta_s / step_s) - 1
        elif step_s == 86400 and 86400 * 4 < delta_s <= 86400 * 10:
            missing_bars_est += 1

    diagnostics = {
        "bar_count": len(bars),
        "start_timestamp": bars[0].timestamp.isoformat() if bars else None,
        "end_timestamp": bars[-1].timestamp.isoformat() if bars else None,
        "duplicate_timestamps": duplicate_timestamps,
        "out_of_order_rows": out_of_order,
        "invalid_or_nan_rows": invalid_rows,
        "estimated_missing_bars": missing_bars_est,
        "sha256": canonical_bars_sha256(bars) if bars else "",
        "integrity_ok": len(bars) >= 60 and out_of_order == 0 and invalid_rows == 0,
    }
    return bars, diagnostics


class DatasetManager:
    """Manages bundled fixtures and imported/captured NSE & NASDAQ research datasets."""

    def __init__(self, storage_dir: Path, *, repo_data_dir: Path | None = None) -> None:
        self.storage_dir = Path(storage_dir) / "datasets"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.repo_data_dir = Path(repo_data_dir) if repo_data_dir is not None else Path("data")

    def _index_path(self) -> Path:
        return self.storage_dir / "index.json"

    def _read_custom_index(self) -> dict[str, dict[str, Any]]:
        path = self._index_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_custom_index(self, index: dict[str, dict[str, Any]]) -> None:
        path = self._index_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def list_datasets(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for spec in BUNDLED_FIXTURES:
            csv_path = self.repo_data_dir / spec["filename"]
            if not csv_path.is_file():
                continue
            bars, diag = self._load_csv_file(csv_path, symbol=spec["symbol"], timeframe=spec["timeframe"])
            _ = bars
            items.append(
                {
                    **spec,
                    **diag,
                    "source_type": "BUNDLED_SYNTHETIC_FIXTURE",
                    "live_eligible": False,
                    "warning": "Bundled synthetic OHLCV fixture for deterministic pipeline verification only — NOT real market data.",
                }
            )
        custom = self._read_custom_index()
        for ds_id in sorted(custom.keys()):
            items.append(custom[ds_id])
        return items

    def _load_csv_file(self, path: Path, *, symbol: str, timeframe: str) -> tuple[list[Candle], dict[str, Any]]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return inspect_and_clean_bars(reader, symbol=symbol, timeframe=timeframe)

    def register_dataset(
        self,
        *,
        dataset_id: str | None = None,
        market: str,
        symbol: str,
        timeframe: str,
        bars_or_csv: list[dict[str, Any] | Candle] | str,
        source: str,
        feed: str,
        corporate_action_adjustment: str = "UNVERIFIED_RAW",
        survivorship_aware: bool = False,
        real_market_data: bool = True,
        corporate_actions: dict[str, Any] | str | bytes | CorporateActionsBook | None = None,
        corporate_action_policy: str = AdjustmentPolicy.RAW.value,
    ) -> dict[str, Any]:
        mkt = (market or "").strip().upper()
        if mkt not in {"NSE", "NASDAQ"}:
            raise ValueError(f"Unsupported market {market!r}; expected NSE or NASDAQ")
        sym = (symbol or "").strip().upper()
        if not sym:
            raise ValueError("symbol is required")
        tf_raw = (timeframe or "1D").strip()
        tf_norm = "1D" if tf_raw.lower() == "1d" else tf_raw
        if tf_norm not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")

        if isinstance(bars_or_csv, str):
            reader = csv.DictReader(io.StringIO(bars_or_csv))
            bars, diag = inspect_and_clean_bars(reader, symbol=sym, timeframe=tf_norm)
        else:
            bars, diag = inspect_and_clean_bars(bars_or_csv, symbol=sym, timeframe=tf_norm)

        if diag["invalid_or_nan_rows"] > 0 or diag["out_of_order_rows"] > 0:
            raise ValueError(
                f"Dataset rejected due to bar corruption: invalid_or_nan={diag['invalid_or_nan_rows']}, "
                f"out_of_order={diag['out_of_order_rows']}"
            )
        if len(bars) < 60:
            raise ValueError(f"Dataset requires at least 60 chronological bars (got {len(bars)})")

        # Validate the action feed before writing anything: a rejected import must
        # leave the dataset store exactly as it was.
        policy = AdjustmentPolicy(str(corporate_action_policy))
        book = self._coerce_book(corporate_actions)

        ds_id = (
            (dataset_id or "").strip()
            or f"ds_{mkt.lower()}_{sym.lower().replace('|', '_')}_{tf_norm}_{diag['sha256'][:10]}"
        )
        safe_id = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in ds_id)
        bars_file = self.storage_dir / f"{safe_id}.json"
        serialized_bars = [
            {
                "timestamp": b.timestamp.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": float(b.volume),
                "symbol": b.symbol,
            }
            for b in bars
        ]
        tmp = bars_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(serialized_bars), encoding="utf-8")
        os.replace(tmp, bars_file)

        actions_file: Path | None = None
        if book is not None:
            actions_file = self.storage_dir / f"{safe_id}.corporate_actions.json"
            tmp_actions = actions_file.with_suffix(".tmp")
            tmp_actions.write_text(
                json.dumps(
                    {
                        "format": "axiom.corporate-actions",
                        "format_version": book.format_version,
                        "provider": book.provider,
                        "actions": [
                            a.canonical() for a in (*book.actions, *book.superseded)
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp_actions, actions_file)
        ca_block = dataset_block(
            bars,
            book,
            policy=policy,
            market=mkt,
            symbol=sym,
            timeframe=tf_norm,
            dataset_sha256=diag["sha256"],
            claim=corporate_action_adjustment,
        )

        tz_name = "Asia/Kolkata" if mkt == "NSE" else "America/New_York"
        meta = {
            "dataset_id": safe_id,
            "market": mkt,
            "symbol": sym,
            "timeframe": tf_norm,
            "timezone": tz_name,
            "source": source,
            "source_type": "IMPORTED_HISTORICAL" if real_market_data else "SYNTHETIC_RESEARCH",
            "feed": feed,
            "corporate_action_adjustment": corporate_action_adjustment,
            "corporate_action_policy": ca_block["policy"],
            "corporate_action_status": ca_block["status"],
            "corporate_actions_adjusted": ca_block["status"]
            in {"SPLIT_ADJUSTED", "DIVIDEND_ADJUSTED"},
            "corporate_actions_verified": bool(ca_block["verified"]),
            "corporate_actions": ca_block,
            "corporate_actions_file": actions_file.name if actions_file else None,
            "survivorship_aware": bool(survivorship_aware),
            "real_market_data": bool(real_market_data),
            "live_eligible": False,
            "created_at": datetime.now(UTC).isoformat(),
            **diag,
            "warning": (
                None
                if survivorship_aware
                else "Single-symbol/point-in-time survivorship awareness not verified; out-of-sample results may overstate multi-constituent returns."
            ),
        }
        index = self._read_custom_index()
        index[safe_id] = meta
        self._write_custom_index(index)
        return meta

    def load_dataset(self, dataset_id: str) -> tuple[dict[str, Any], list[Candle]]:
        for spec in BUNDLED_FIXTURES:
            if spec["dataset_id"] == dataset_id:
                csv_path = self.repo_data_dir / spec["filename"]
                if not csv_path.is_file():
                    raise KeyError(f"Bundled dataset file missing: {spec['filename']}")
                bars, diag = self._load_csv_file(csv_path, symbol=spec["symbol"], timeframe=spec["timeframe"])
                meta = {
                    **spec,
                    **diag,
                    "source_type": "BUNDLED_SYNTHETIC_FIXTURE",
                    "live_eligible": False,
                }
                return meta, bars

        index = self._read_custom_index()
        if dataset_id not in index:
            raise KeyError(f"Unknown dataset_id {dataset_id!r}")
        meta = index[dataset_id]
        bars_file = self.storage_dir / f"{dataset_id}.json"
        if not bars_file.is_file():
            raise KeyError(f"Dataset payload missing for {dataset_id!r}")
        rows = json.loads(bars_file.read_text(encoding="utf-8"))
        bars, actual_diag = inspect_and_clean_bars(rows, symbol=meta["symbol"], timeframe=meta["timeframe"])
        if actual_diag["sha256"] != meta.get("sha256"):
            raise ValueError(
                f"Dataset SHA-256 mismatch for {dataset_id}: expected {meta.get('sha256')}, got {actual_diag['sha256']}"
            )
        return meta, bars


    @staticmethod
    def _coerce_book(
        corporate_actions: dict[str, Any] | str | bytes | CorporateActionsBook | None,
    ) -> CorporateActionsBook | None:
        """Accept an already-validated book or a raw provider-neutral payload.

        Structural problems, duplicates, conflicts and ambiguities raise
        :class:`app.data.corporate_actions.CorporateActionError` (a ``ValueError``),
        which fails the import instead of storing an unusable adjustment.
        """
        if corporate_actions is None:
            return None
        if isinstance(corporate_actions, CorporateActionsBook):
            return corporate_actions
        return CorporateActionsBook.from_payload(corporate_actions)

    def load_corporate_actions(self, dataset_id: str) -> CorporateActionsBook | None:
        """Load the verified action book for a dataset and re-verify its fingerprint."""
        index = self._read_custom_index()
        meta = index.get(dataset_id)
        if meta is None:
            return None
        filename = meta.get("corporate_actions_file")
        if not filename:
            return None
        path = self.storage_dir / str(filename)
        if not path.is_file():
            raise KeyError(f"Corporate-action payload missing for {dataset_id!r}")
        book = CorporateActionsBook.from_payload(path.read_bytes())
        expected = (meta.get("corporate_actions") or {}).get("actions_sha256")
        if expected and book.actions_sha256() != expected:
            raise ValueError(
                f"Corporate-action SHA-256 mismatch for {dataset_id}: expected {expected}, "
                f"got {book.actions_sha256()}"
            )
        return book

    def load_adjusted_dataset(
        self,
        dataset_id: str,
        *,
        policy: AdjustmentPolicy | str | None = None,
        as_of: datetime | None = None,
    ):
        """Return ``(meta, AdjustedSeries)`` for the requested research convention.

        The dataset on disk always stays raw; the adjusted series is derived per call so
        the same dataset can serve a raw paper path and an adjusted research path without
        either being able to masquerade as the other.
        """
        from app.data.corporate_actions import build_series

        meta, bars = self.load_dataset(dataset_id)
        block = meta.get("corporate_actions") or {}
        chosen = AdjustmentPolicy(str(policy or block.get("policy") or "RAW"))
        book = self.load_corporate_actions(dataset_id)
        series = build_series(
            bars,
            book,
            policy=chosen,
            as_of=as_of,
            market=str(meta.get("market") or ""),
            symbol=str(meta.get("symbol") or ""),
            timeframe=str(meta.get("timeframe") or ""),
            dataset_sha256=str(meta.get("sha256") or ""),
        )
        return {**meta, "corporate_actions": {**block, **series.provenance}}, series
