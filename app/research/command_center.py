"""CommandCenter: natural-language answers over RECORDED data only.

Every answer is computed from stored rows and labeled honestly
(OBSERVED / CALCULATED / MODEL INTERPRETATION / HONEST). Unknown questions
get an explicit topic fallback — statistics are never invented.
"""

from __future__ import annotations

import json


def _load(payload):
    return json.loads(payload) if isinstance(payload, str) else (payload or {})


class CommandCenter:
    def __init__(self, repository):
        self.repository = repository

    def ask(self, question: str) -> dict:
        text = (question or "").lower()
        if "why" in text and ("lose" in text or "loss" in text or "lost" in text or "down" in text):
            return self._why_losing()
        if "what changed" in text or "changes" in text:
            return self._what_changed()
        if "degrad" in text:
            return self._degrading()
        if "contribut" in text and ("p&l" in text or "pnl" in text or "profit" in text):
            return self._attribution_answer()
        if "exposure" in text or "risk" in text:
            return self._risk_answer()
        if "anomal" in text or "unusual" in text:
            return self._anomaly_answer()
        return {"answer": "No matching recorded data for that question. Ask about: "
                          "why Arena lost money, what changed, which strategies are "
                          "degrading, what contributed to P&L, current exposure/risk, "
                          "or recent anomalies.",
                "labels": ["HONEST"]}

    # -- intents ---------------------------------------------------------------

    def _why_losing(self) -> dict:
        from app.research.attribution import Attribution

        result = Attribution(self.repository).attribute()
        if result["total_trades"] == 0:
            return {"answer": "No recorded trade outcomes are available, so there "
                              "is no recorded trade loss to explain (OBSERVED).",
                    "labels": ["OBSERVED"]}
        lines = [f"NET P&L is {result['total_net_pnl']:.2f} over "
                 f"{result['total_trades']} trades (CALCULATED from recorded outcomes)."]
        strategy_dim = result["dimensions"].get("strategy", {})
        for name, stats in sorted(strategy_dim.items(),
                                  key=lambda kv: kv[1]["net_pnl"])[:5]:
            lines.append(f"- {name}: NET {stats['net_pnl']:.2f} over "
                         f"{stats['trades']} trades (CALCULATED)")
        lines.append("This explains recorded outcomes only; it is not a forecast.")
        return {"answer": "\n".join(lines),
                "labels": ["OBSERVED", "CALCULATED"]}

    def _what_changed(self) -> dict:
        events = self.repository.list_system_events(limit=200)
        learning = self.repository.list_learning_events(limit=200)
        kinds: dict[str, int] = {}
        for event in events:
            kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
        for event in learning:
            kinds[f"learning:{event['kind']}"] = \
                kinds.get(f"learning:{event['kind']}", 0) + 1
        if not kinds:
            return {"answer": "No recorded changes in the stored event history "
                              "(OBSERVED).",
                    "labels": ["OBSERVED"]}
        top = sorted(kinds.items(), key=lambda kv: kv[1], reverse=True)[:8]
        lines = ["Recent recorded activity (OBSERVED event counts):"]
        lines += [f"- {kind}: {count}" for kind, count in top]
        return {"answer": "\n".join(lines), "labels": ["OBSERVED"]}

    def _degrading(self) -> dict:
        rows = []
        for lineage in self.repository.list_lineage():
            current = self.repository.current_lifecycle(lineage["id"])
            if current and current["status"] in {"PROBATION", "SHADOW", "RETIRED"}:
                rows.append(f"- {lineage['strategy']}: {current['status']} "
                            f"(stage {current['stage']})")
        if not rows:
            return {"answer": "No strategies are currently recorded as degrading "
                              "(OBSERVED from lifecycle history).",
                    "labels": ["OBSERVED"]}
        return {"answer": "Strategies with recorded degradation:\n" + "\n".join(rows),
                "labels": ["OBSERVED"]}

    def _attribution_answer(self) -> dict:
        from app.research.attribution import Attribution

        result = Attribution(self.repository).attribute()
        if result["total_trades"] == 0:
            return {"answer": "No recorded trade outcomes in scope (OBSERVED).",
                    "labels": ["OBSERVED"]}
        lines = ["What contributed to P&L (CALCULATED from recorded outcomes):"]
        strategy_dim = result["dimensions"].get("strategy", {})
        for name, stats in sorted(strategy_dim.items(),
                                  key=lambda kv: kv[1]["net_pnl"]):
            lines.append(f"- {name}: NET {stats['net_pnl']:.2f} over "
                         f"{stats['trades']} trades")
        lines.append(f"Total NET: {result['total_net_pnl']:.2f}")
        return {"answer": "\n".join(lines), "labels": ["CALCULATED", "OBSERVED"]}

    def _risk_answer(self) -> dict:
        stop = self.repository.get_engine_state("emergency_stop") or {}
        allocation = self.repository.get_engine_state("capital_allocation") or {}
        exposure = self.repository.get_engine_state("sandbox_equity") or {}
        lines = ["Current recorded risk state (OBSERVED):",
                 f"- emergency stop: {'ACTIVE' if stop.get('stopped') else 'clear'}",
                 f"- recorded equity: {exposure.get('equity', '—')}",
                 f"- capital allocation rows: "
                 f"{len(allocation.get('allocations', {})) if isinstance(allocation, dict) else '—'}",
                 "Hard risk limits live in configuration and cannot be changed "
                 "by learning or from here."]
        return {"answer": "\n".join(lines), "labels": ["OBSERVED"]}

    def _anomaly_answer(self) -> dict:
        anomalies = [e for e in self.repository.list_learning_events(limit=200)
                     if e["kind"] == "ANOMALY_DETECTED"]
        if not anomalies:
            return {"answer": "No anomalies recorded in the stored event history "
                              "(OBSERVED).",
                    "labels": ["OBSERVED"]}
        lines = ["Recent recorded anomalies (OBSERVED):"]
        for event in anomalies[:8]:
            payload = _load(event["payload"])
            lines.append(f"- {event['ts'][:16]}: {payload.get('kind')}")
        return {"answer": "\n".join(lines), "labels": ["OBSERVED"]}
