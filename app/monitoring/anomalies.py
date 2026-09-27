"""AnomalyDetector: operational anomalies from RECORDED events only.

Checks (thresholds documented, deliberately conservative):
  - order_frequency_spike: ≥N orders inside one hour
  - losing_streak: ≥K consecutive negative recorded trade outcomes per strategy
  - feed_failures: ≥F consecutive FEED_ERROR / SANDBOX_RECONCILE_ERROR
    (reset by SANDBOX_FILL_RECONCILED / MARKET_CLOSED_SKIP)
  - broker_mismatch: orders stuck SUBMITTED/PENDING_SUBMIT > 6h
  - missing_data: coverage rows with 0 records
Dangerous anomalies (order spike, feed failures) auto-pause the execution
component via PauseGate — a SAFE, persisted action; risk limits untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _load(payload):
    return json.loads(payload) if isinstance(payload, str) else payload


def _ts(row) -> object:
    value = row.get("ts") or row.get("created_ts")
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


class AnomalyDetector:
    def __init__(self, repository, *, pause_gate=None):
        self.repository = repository
        self.pause_gate = pause_gate

    def detect(self, *, max_orders_per_hour: int = 30,
               max_consecutive_feed_failures: int = 5,
               max_losing_streak: int = 6) -> list[dict]:
        anomalies: list[dict] = []
        events = self.repository.list_system_events(limit=1000)
        # 1. order frequency spike
        order_events = [e for e in events if e["kind"] == "SANDBOX_ORDER_SUBMITTED"]
        if len(order_events) >= max_orders_per_hour:
            window = order_events[:max_orders_per_hour]
            span = (_ts(window[0]) - _ts(window[-1])).total_seconds() / 3600
            if span < 1.0:
                anomalies.append({"kind": "order_frequency_spike",
                                  "orders": max_orders_per_hour, "hours": span})
        # 2. losing streak per strategy
        outcomes = [e for e in events if e["kind"] == "TRADE_OUTCOME"]
        streaks: dict[str, int] = {}
        for event in outcomes:
            payload = _load(event["payload"])
            name = str(payload.get("strategy"))
            streaks[name] = streaks.get(name, 0) + 1 if float(
                payload.get("pnl") or 0) < 0 else 0
        for name, streak in streaks.items():
            if streak >= max_losing_streak:
                anomalies.append({"kind": "losing_streak", "strategy": name,
                                  "streak": streak})
        # 3. consecutive feed failures
        feed_failures = 0
        for event in events:  # events are newest-first
            if event["kind"] in {"FEED_ERROR", "SANDBOX_RECONCILE_ERROR"}:
                feed_failures += 1
                if feed_failures >= max_consecutive_feed_failures:
                    anomalies.append({"kind": "feed_failures",
                                      "consecutive": feed_failures})
                    break
            elif event["kind"] in {"SANDBOX_FILL_RECONCILED", "MARKET_CLOSED_SKIP"}:
                feed_failures = 0
        # 4. broker mismatch: submitted orders whose status never advanced
        now = datetime.now(timezone.utc)
        for order in self.repository.list_sandbox_orders(limit=200):
            if order["status"] in {"SUBMITTED", "PENDING_SUBMIT"}:
                created = _ts(order)
                age_hours = (now - created).total_seconds() / 3600
                if age_hours > 6:
                    anomalies.append({"kind": "broker_mismatch",
                                      "order": order["id"],
                                      "hours_stuck": round(age_hours, 1)})
                    break
        # 5. coverage regression
        for row in self.repository.list_coverage(limit=100):
            if int(row["records"] or 0) == 0:
                anomalies.append({"kind": "missing_data",
                                  "instrument_key": row["instrument_key"]})
                break
        for anomaly in anomalies:
            self.repository.record_learning_event("ANOMALY_DETECTED", anomaly)
        if anomalies and self.pause_gate is not None:
            kinds = {a["kind"] for a in anomalies}
            if "order_frequency_spike" in kinds:
                self.pause_gate.pause("execution", "anomaly: order frequency spike")
            if "feed_failures" in kinds:
                self.pause_gate.pause("execution", "anomaly: consecutive feed failures")
        return anomalies
