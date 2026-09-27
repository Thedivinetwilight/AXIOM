"""DecisionReplay: reconstruct any decision from RECORDED rows only.

Replay uses exclusively what was recorded at or before the decision
timestamp: the signal row, the regime recorded then, the params version in
force, and the risk/order event in the same cycle. Information that was
never recorded is marked unavailable — never reconstructed after the fact.
"""

from __future__ import annotations

import json
from datetime import datetime


def _load(payload):
    return json.loads(payload) if isinstance(payload, str) else (payload or {})


def _within(event_ts: str, signal_ts: str, *, seconds: int) -> bool:
    try:
        event = datetime.fromisoformat(str(event_ts))
        signal = datetime.fromisoformat(str(signal_ts))
    except (TypeError, ValueError):
        return False
    return 0 <= (event - signal).total_seconds() <= seconds


class DecisionReplay:
    def __init__(self, repository):
        self.repository = repository

    def replay_signal(self, signal_id: str) -> dict:
        signal = next((s for s in self.repository.list_signals(limit=2000)
                       if s["id"] == signal_id), None)
        if signal is None:
            return {"found": False, "reason": "signal id not present in recorded rows"}
        ts = signal["ts"]
        payload = _load(signal.get("payload"))
        replay = {
            "found": True,
            "signal": {"id": signal["id"], "ts": ts, "strategy": signal["strategy"],
                       "params_version": signal["params_version"],
                       "symbol": signal["symbol"], "signal": signal["signal"],
                       "price": signal["price"],
                       "regime_recorded_then": signal.get("regime"),
                       "meta": payload.get("meta", {})},
            "known_at_decision": {
                "regime": signal.get("regime"),
                "params_version": signal["params_version"],
                "price": signal["price"],
                "note": "only rows recorded at or before the decision timestamp"},
        }
        # risk decision recorded within 120s after the signal (same cycle)
        risk_events = [e for e in self.repository.list_system_events(limit=1000)
                       if e["kind"] in {"RISK_REJECT", "SANDBOX_ORDER_SUBMITTED"}
                       and _within(e["ts"], ts, seconds=120)]
        replay["risk_decision"] = (
            {"kind": e["kind"], "ts": e["ts"], "payload": _load(e["payload"])}
            for e in risk_events[:1])
        replay["risk_decision"] = next(iter(replay["risk_decision"]), None)
        replay["disclaimer"] = ("Replay uses recorded rows only; information that "
                                "was never recorded is marked unavailable, never "
                                "reconstructed after the fact.")
        return replay

    def explain_outcome(self, outcome_event_ts: str, strategy: str) -> dict:
        """Nearest recorded signal + risk decision that explains an outcome."""
        outcome_ts = str(outcome_event_ts)
        candidates = [s for s in self.repository.list_signals(limit=2000)
                      if s["strategy"] == strategy and _within(outcome_ts, s["ts"],
                                                               seconds=3600)]
        if not candidates:
            return {"found": False,
                    "reason": "no recorded signal within the lookback window"}
        candidates.sort(key=lambda s: s["ts"])
        signal = candidates[-1]
        replay = self.replay_signal(signal["id"])
        return {"found": True, "signal_id": signal["id"],
                "replay": replay, "outcome_ts": outcome_ts}
