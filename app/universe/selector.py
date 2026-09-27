"""UniverseSelector: ALL known instruments ≠ ELIGIBLE trading universe.

Filters (all explicit, none automatic): exchange, segment, instrument types,
active state, minimum local history (records), price range, liquidity proxy
(mean volume × close over recent history) and custom include/exclude symbol
lists. The eligible universe is only ever a research/streaming selection —
trading still requires the hard risk gate per order.
"""

from __future__ import annotations

import statistics

from app.history.store import CandleStore


class UniverseSelector:
    def __init__(self, repository, store: CandleStore | None = None):
        self.repository = repository
        self.store = store

    def screen(self, *, exchange: str | None = "NSE", segment: str | None = None,
               instrument_types: tuple[str, ...] = ("EQ",), state: str = "active",
               min_history_records: int = 0, min_price: float | None = None,
               max_price: float | None = None, min_avg_volume: float | None = None,
               unit: str = "days", interval: int = 1, include_symbols: set | None = None,
               exclude_symbols: set | None = None, scan_limit: int = 5000) -> dict:
        candidates = self.repository.list_instruments(
            exchange=exchange, segment=segment, instrument_type=None,
            state=state, limit=scan_limit)
        eligible, rejected = [], []
        for row in candidates:
            symbol = row["trading_symbol"] or ""
            if instrument_types and row["instrument_type"] not in instrument_types:
                rejected.append({"instrument_key": row["instrument_key"],
                                 "why": "type", "value": row["instrument_type"]})
                continue
            if include_symbols is not None and symbol not in include_symbols:
                rejected.append({"instrument_key": row["instrument_key"], "why": "not_included"})
                continue
            if exclude_symbols and symbol in exclude_symbols:
                rejected.append({"instrument_key": row["instrument_key"], "why": "excluded"})
                continue
            history_ok, stats = self._check_history(
                row["instrument_key"], unit, interval, min_history_records,
                min_price, max_price, min_avg_volume)
            if not history_ok:
                rejected.append({"instrument_key": row["instrument_key"],
                                 "why": "history", **stats})
                continue
            eligible.append({"instrument_key": row["instrument_key"],
                             "trading_symbol": symbol, "exchange": row["exchange"],
                             "stats": stats})
        return {"eligible": eligible, "rejected_sample": rejected[:50],
                "eligible_count": len(eligible), "scanned": len(candidates),
                "note": "eligible = research/streaming selection; trading still "
                        "requires the per-order hard risk gate"}

    def _check_history(self, instrument_key: str, unit: str, interval: int,
                       min_records: int, min_price, max_price,
                       min_avg_volume) -> tuple[bool, dict]:
        stats: dict = {}
        if not self.store or (min_records <= 0 and min_price is None
                              and max_price is None and min_avg_volume is None):
            return True, stats
        rows = self.store.read_range(instrument_key, unit, int(interval))
        stats["records"] = len(rows)
        if len(rows) < min_records:
            return False, stats
        if rows:
            closes = [r["close"] for r in rows]
            volumes = [r["volume"] for r in rows]
            stats["last_close"] = closes[-1]
            stats["avg_volume"] = statistics.fmean(volumes) if volumes else 0.0
            if min_price is not None and closes[-1] < min_price:
                return False, stats
            if max_price is not None and closes[-1] > max_price:
                return False, stats
            if min_avg_volume is not None and stats["avg_volume"] < min_avg_volume:
                return False, stats
        elif min_records > 0:
            return False, stats
        return True, stats
