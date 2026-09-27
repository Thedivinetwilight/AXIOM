"""TradeIntelligence: aggregate recorded outcomes by strategy × regime × time.

Uses ONLY recorded journal data (trade_outcome events, signals, shadow
trades). Produces observable statistics: win rate, expectancy, profit
factor, average duration and time-of-day buckets. No fitting, no look-ahead.
"""

from __future__ import annotations

from statistics import fmean


def profit_factor(pnls: list[float]) -> float | None:
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    return wins / losses if losses else None


class TradeIntelligence:
    def __init__(self, repository):
        self.repository = repository

    def outcomes(self) -> list[dict]:
        import json

        events = [e for e in self.repository.list_system_events(limit=1000)
                  if e["kind"] == "TRADE_OUTCOME"]
        return [e["payload"] if isinstance(e["payload"], dict)
                else json.loads(e["payload"]) for e in events]

    def summarize(self) -> dict:
        rows = self.outcomes()
        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            groups.setdefault((row.get("strategy"), row.get("regime")), []).append(row)
        summary = {}
        for (strategy, regime), items in sorted(groups.items(), key=lambda kv: str(kv[0])):
            pnls = [item["pnl"] for item in items]
            summary[f"{strategy}|{regime}"] = {
                "trades": len(items),
                "win_rate": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0.0,
                "expectancy": fmean(pnls) if pnls else 0.0,
                "profit_factor": profit_factor(pnls),
                "avg_duration_bars": fmean([item.get("duration_bars", 0) for item in items]),
            }
        return summary
