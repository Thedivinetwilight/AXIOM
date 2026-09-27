"""P&L attribution over RECORDED trade outcomes.

Dimensions: strategy, params version, instrument, regime, holding duration
(short<=5 / medium<=20 / long>20 bars). Everything is CALCULATED from
TRADE_OUTCOME events; nothing is estimated or fabricated.
"""

from __future__ import annotations

import json


def _load(payload):
    return json.loads(payload) if isinstance(payload, str) else (payload or {})


def duration_bucket(bars: float) -> str:
    if bars <= 5:
        return "short<=5"
    if bars <= 20:
        return "medium<=20"
    return "long>20"


class Attribution:
    def __init__(self, repository):
        self.repository = repository

    def outcomes(self) -> list[dict]:
        return [_load(e["payload"]) for e in self.repository.list_system_events(limit=2000)
                if e["kind"] == "TRADE_OUTCOME"]

    def attribute(self) -> dict:
        rows = self.outcomes()
        dimensions: dict[str, dict] = {}
        for row in rows:
            groups = {
                "strategy": str(row.get("strategy")),
                "version": str(row.get("params_version") or row.get("strategy")),
                "instrument": str(row.get("symbol")),
                "regime": str(row.get("regime")),
                "duration": duration_bucket(float(row.get("duration_bars") or 0)),
            }
            for dimension, key in groups.items():
                bucket = dimensions.setdefault(dimension, {})
                entry = bucket.setdefault(key, {"net_pnl": 0.0, "fees": 0.0,
                                                "trades": 0, "wins": 0})
                entry["net_pnl"] += float(row.get("pnl") or 0)
                entry["fees"] += float(row.get("fees") or 0)
                entry["trades"] += 1
                entry["wins"] += 1 if float(row.get("pnl") or 0) > 0 else 0
        return {"dimensions": dimensions,
                "total_net_pnl": sum(float(r.get("pnl") or 0) for r in rows),
                "total_trades": len(rows),
                "labels": {"net_pnl": "CALCULATED from recorded trade outcomes"}}
