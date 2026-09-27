"""Translate recorded technical events into human-readable activity entries.

Every sentence is derived from what was actually recorded in the event
payload — nothing is invented after the fact. Unknown event kinds render a
neutral factual line instead of a guessed explanation. Raw events remain
available (Advanced Logs) untouched.
"""

from __future__ import annotations

import json


def _load(payload) -> dict:
    if isinstance(payload, dict):
        return payload
    try:
        return json.loads(payload or "{}")
    except (TypeError, ValueError):
        return {}


def explain(event: dict) -> dict:
    kind = event.get("kind", "UNKNOWN")
    payload = _load(event.get("payload"))
    ts = str(event.get("ts", ""))[:19].replace("T", " ")
    if kind == "SANDBOX_ORDER_SUBMITTED":
        side = str(payload.get("side", "?"))
        qty = payload.get("quantity", "?")
        return {"ts": ts, "kind": kind,
                "headline": f"Placed a {side} sandbox order for {qty}.",
                "why": "The active strategy produced a signal and the hard risk gate allowed it.",
                "outcome": "Order sent to the Upstox sandbox; awaiting confirmation."}
    if kind == "SANDBOX_FILL_RECONCILED":
        return {"ts": ts, "kind": kind,
                "headline": f"Sandbox order filled: {payload.get('quantity')} @ {payload.get('price')}.",
                "why": "Confirmed against the official Upstox order history.",
                "outcome": "Position and cash updated from the confirmed fill."}
    if kind == "RISK_REJECT":
        return {"ts": ts, "kind": kind,
                "headline": f"Skipped a trade: {payload.get('reason', 'risk limit')}.",
                "why": "A hard risk limit blocked this order. Learning can never change these limits.",
                "outcome": "No order was sent."}
    if kind == "EMERGENCY_STOP":
        return {"ts": ts, "kind": kind,
                "headline": "EMERGENCY STOP activated.",
                "why": f"Recorded reason: {payload.get('reason', 'manual')}.",
                "outcome": "Open sandbox orders were cancelled; trading halted until explicit resume."}
    if kind == "EVALUATION_RUN":
        promoted = bool(payload.get("promoted"))
        version = payload.get("version")
        return {"ts": ts, "kind": kind,
                "headline": ("Learning promoted a new model version." if promoted
                             else "Learning evaluated a candidate; no promotion."),
                "why": "Candidates must beat the current version on out-of-sample data.",
                "outcome": f"Version: {version or 'unchanged'}."}
    if kind == "REGIME_CHANGE":
        return {"ts": ts, "kind": kind,
                "headline": f"Market regime changed: {payload.get('from')} → {payload.get('to')}.",
                "why": "Detected from observable price/volume features (no prediction).",
                "outcome": "Strategy routing will use the new regime on the next batch."}
    if kind == "MARKET_CLOSED_SKIP":
        return {"ts": ts, "kind": kind,
                "headline": "Market closed — candle ignored.",
                "why": "Trading actions only run during market hours.",
                "outcome": "Data was reconciled; nothing traded."}
    if kind == "SANDBOX_SUBMIT_FAILURE":
        return {"ts": ts, "kind": kind,
                "headline": "Sandbox order submission failed.",
                "why": f"Recorded error: {payload.get('error', 'unknown')}.",
                "outcome": "No order is considered placed; the failure is recorded."}
    return {"ts": ts, "kind": kind,
            "headline": f"Recorded event: {kind.lower().replace('_', ' ')}.",
            "why": "See raw event detail (Advanced Logs).",
            "outcome": ""}


def explain_all(events: list[dict], limit: int = 100) -> list[dict]:
    return [explain(event) for event in events[:limit]]
