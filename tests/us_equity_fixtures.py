"""Synthetic dataset builder used ONLY by tests.

These prices are deterministic fixtures for exercising the harness code
(validation, timing, cost accounting, fold slicing). They are never written to
`results/`, never printed as a result, and never referenced by any report: the
research harness refuses to run without a real, licensed, point-in-time dataset.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.markets.us_calendar import USEquitiesCalendar

NY = ZoneInfo("America/New_York")


def session_days(start: date, count: int) -> list[date]:
    calendar = USEquitiesCalendar()
    days, day = [], start
    while len(days) < count:
        if calendar.session_bounds(day) is not None:
            days.append(day)
        day += timedelta(days=1)
    return days


def close_stamp(day: date) -> str:
    calendar = USEquitiesCalendar()
    return calendar.session_bounds(day)[1].astimezone(ZoneInfo("UTC")).isoformat()


def build_dataset(
    root: Path,
    *,
    symbols: tuple[str, ...] = ("AAA", "BBB", "CCC", "DDD", "SPY"),
    sessions: int = 420,
    start: date = date(2021, 1, 4),
    seed: int = 7,
    drift: dict[str, float] | None = None,
    universe: str = "point_in_time",
    late_listing: dict[str, int] | None = None,
    stamp_hour_shift: int = 0,
    omit_manifest_fields: tuple[str, ...] = (),
    adjustment: str = "split_dividend",
    with_actions: bool = True,
    volume: float = 5_000_000.0,
    benchmark: str | None = "SPY",
) -> Path:
    """Write a dataset directory satisfying the documented contract."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    days = session_days(start, sessions)
    rng = random.Random(seed)
    drift = drift or {symbol: 0.0006 - 0.0001 * index for index, symbol in enumerate(symbols)}
    late_listing = late_listing or {}

    rows = []
    for symbol in symbols:
        price = 40.0 + 10 * symbols.index(symbol)
        first_index = late_listing.get(symbol, 0)
        for index, day in enumerate(days):
            if index < first_index:
                continue
            step = rng.gauss(drift.get(symbol, 0.0), 0.012)
            open_price = price
            close_price = max(1.0, price * (1 + step))
            high = max(open_price, close_price) * (1 + abs(rng.gauss(0, 0.003)))
            low = min(open_price, close_price) * (1 - abs(rng.gauss(0, 0.003)))
            stamp = close_stamp(day)
            if stamp_hour_shift:
                moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                moment += timedelta(hours=stamp_hour_shift)
                stamp = moment.isoformat()
            rows.append({
                "symbol": symbol, "timestamp": stamp,
                "open": round(open_price, 4), "high": round(high, 4),
                "low": round(low, 4), "close": round(close_price, 4),
                "volume": volume,
            })
            price = close_price

    with (root / "bars.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    if universe == "point_in_time":
        with (root / "universe.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["symbol", "start", "end"])
            for symbol in symbols:
                begin = days[late_listing.get(symbol, 0)]
                writer.writerow([symbol, begin.isoformat(), days[-1].isoformat()])

    if with_actions:
        with (root / "corporate_actions.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["symbol", "ex_date", "type", "ratio", "amount"])
            writer.writerow([symbols[0], days[len(days) // 2].isoformat(), "split", "2", ""])
            writer.writerow([symbols[0], days[len(days) // 3].isoformat(), "dividend", "", "0.25"])

    manifest = {
        "schema": "axiom.us.equities.dataset/1",
        "source": {
            "name": "TEST FIXTURE (not market data)",
            "license": "internal test fixture",
            "url": "https://example.invalid/fixture",
            "retrieved_at": "2026-01-01T00:00:00+00:00",
            "feed": "fixture",
        },
        "adjustment": adjustment,
        "price_basis": "total_return_prices",
        "bar_timeframe": "1D",
        "bar_timestamp_convention": "session_close",
        "timezone": "UTC",
        "benchmark": benchmark,
        "universe": {
            "kind": universe,
            "membership_csv": "universe.csv" if universe == "point_in_time" else "",
            "description": "deterministic test fixture; not a real universe",
        },
        "bars_csv": "bars.csv",
    }
    if with_actions:
        manifest["corporate_actions_csv"] = "corporate_actions.csv"
    for field in omit_manifest_fields:
        manifest.pop(field, None)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return root
