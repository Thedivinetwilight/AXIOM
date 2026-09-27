"""Pure-Python view models: backend payloads → display rows.

Kept Qt-free so the desktop logic is testable anywhere. Money formatting is
explicit about OBSERVED vs CALCULATED labels where the report requires it.
"""

from __future__ import annotations


def money(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def overview_rows(data: dict) -> list[tuple[str, str]]:
    latest = data.get("latest_snapshot") or {}
    equity = data.get("equity")
    initial = data.get("capital_initial")
    total = None
    if equity is not None and initial:
        total = float(equity) - float(initial)
    return [
        ("Execution mode", str(data.get("mode_display", "—"))),
        ("Emergency stop", "ACTIVE" if data.get("emergency_stop") else "clear"),
        ("Equity", money(equity)),
        ("Capital (initial)", money(initial)),
        ("Total P&L", money(total) + (" (CALCULATED)" if total is not None else "")),
        ("Return %", pct(total / initial if total is not None and initial else None)),
        ("Drawdown", pct(data.get("drawdown"))),
        ("Daily P&L (realized)", money(latest.get("realized"))),
        ("Daily P&L (unrealized)", money(latest.get("unrealized"))),
    ]


def trade_rows(data: dict) -> list[list[str]]:
    header = ["time", "instrument", "action", "qty", "price", "mode"]
    rows = [header]
    for fill in data.get("fills", []):
        rows.append([str(fill.get("ts", ""))[:19].replace("T", " "),
                     str(fill.get("symbol", "—")), str(fill.get("side", "—")),
                     money(fill.get("quantity")), money(fill.get("price")),
                     "UPSTOX_SANDBOX"])
    return rows


def risk_rows(data: dict) -> list[tuple[str, str]]:
    limits = data.get("limits") or {}
    rows = [
        ("Emergency stop", "ACTIVE" if data.get("emergency_stop") else "clear"),
        ("Equity", money(data.get("equity"))),
        ("Drawdown", pct(data.get("drawdown"))),
        ("Risk events (recorded)", str(data.get("risk_events", 0))),
    ]
    for name, value in limits.items():
        rows.append((name, "unlimited" if not value else money(value)))
    rows.append(("Note", str(data.get("limits_note", ""))))
    return rows


def market_data_rows(data: dict) -> list[tuple[str, str]]:
    universe = data.get("universe") or {}
    coverage = data.get("coverage") or []
    rows = [
        ("Instruments (total)", str(universe.get("total", 0))),
        ("Active", str(universe.get("state_active", 0))),
        ("Removed", str(universe.get("state_removed", 0))),
        ("Exchanges", ", ".join(f"{k}:{v}" for k, v in
                                (universe.get("by_exchange") or {}).items()) or "—"),
    ]
    for row in coverage[:10]:
        completeness = float(row.get("completeness") or 0)
        label = "n/a (calendar)" if completeness < 0 else pct(completeness)
        rows.append((f"{row['instrument_key']} {row['unit']}/{row['interval']}",
                     f"{row['records']} candles · {label}"))
    return rows


def learning_rows(data: dict) -> list[tuple[str, str]]:
    rows = []
    for version in (data.get("model_versions") or [])[:8]:
        rows.append((f"model {version['version'][:40]}",
                     ("PROMOTED" if version["promoted"] else "candidate")))
    for event in (data.get("learning_events") or [])[:8]:
        payload = event.get("payload")
        detail = payload.get("version") if isinstance(payload, dict) else ""
        rows.append((f"{str(event.get('ts', ''))[:16]} {event.get('kind', '')}",
                     str(detail or "")))
    return rows


def experiment_rows(data: dict) -> list[list[str]]:
    header = ["experiment", "status", "candidates", "promoted"]
    rows = [header]
    for exp in data.get("experiments", []):
        result = exp.get("result")
        result = json_loads(result) if isinstance(result, str) else (result or {})
        rows.append([exp.get("name", "—"), exp.get("status", "—"),
                     str(result.get("candidates", "—")),
                     str(result.get("promoted", "—"))])
    return rows


def json_loads(text):
    import json

    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {}


# ----------------------------------------------------------------- FINAL ----

def command_center_view(answer: dict) -> list[tuple[str, str]]:
    return [("Answer", str(answer.get("answer", "—"))),
            ("Labels", ", ".join(answer.get("labels", [])))]


def portfolio_rows(data: dict) -> list[tuple[str, str]]:
    snapshot = data.get("snapshot") or {}
    rows = [
        ("Portfolio value", money(snapshot.get("equity"))),
        ("Gross exposure", money(snapshot.get("gross_exposure"))),
        ("Exposure ratio", pct(snapshot.get("exposure_ratio"))),
        ("Max concentration", pct(snapshot.get("max_concentration"))),
        ("Open positions", str(snapshot.get("positions", 0))),
        ("Realized P&L", money(snapshot.get("realized_pnl"))),
        ("Unrealized P&L", money(snapshot.get("unrealized_pnl"))),
        ("Hard cap per strategy", pct(data.get("hard_cap"))),
    ]
    allocation = data.get("allocation") or {}
    if isinstance(allocation, dict):
        for strategy, amount in (allocation.get("allocations") or {}).items():
            rows.append((f"allocated: {strategy}", money(amount)))
    if snapshot.get("note"):
        rows.append(("Note", str(snapshot["note"])))
    return rows


def attribution_values(data: dict) -> list[tuple[str, float]]:
    dimensions = (data or {}).get("dimensions") or {}
    return [(name, round(stats.get("net_pnl", 0.0), 2))
            for name, stats in (dimensions.get("strategy") or {}).items()]


def research_lab_rows(data: dict) -> list[tuple[str, str]]:
    rows = []
    for experiment in (data.get("experiments") or [])[:8]:
        result = experiment.get("result")
        result = json_loads(result) if isinstance(result, str) else (result or {})
        rows.append((f"experiment {experiment.get('name', '')}",
                     f"{experiment.get('status')} · candidates={result.get('candidates', '—')}"))
    for question in (data.get("questions") or [])[:8]:
        rows.append((f"question: {question.get('question', '')[:60]}",
                     str(question.get("status", ""))))
    for lineage in (data.get("lineage") or [])[:8]:
        rows.append((f"lineage {lineage.get('strategy')} gen{lineage.get('generation')}",
                     str(lineage.get("id"))[:12]))
    return rows


def market_intelligence_rows(data: dict) -> list[tuple[str, str]]:
    rows = market_data_rows(data)
    regimes = data.get("regimes") or []
    for row in regimes[:5]:
        payload = row.get("features")
        payload = json_loads(payload) if isinstance(payload, str) else (payload or {})
        rows.append((f"regime@{str(row.get('ts'))[:16]}", str(row.get("regime"))))
    rows.append(("Regime similarity",
                 str(data.get("regime_similarity_disclaimer", ""))[:80]))
    return rows


def learning_detail_rows(data: dict) -> list[tuple[str, str]]:
    rows = []
    calibration = data.get("calibration") or {}
    for strategy, stats in calibration.items():
        rows.append((f"calibration {strategy}",
                     f"n={stats.get('n')} brier={stats.get('brier')}"))
    rows.append(("regime history rows", str(data.get("regime_history_count", 0))))
    shadow = data.get("shadow_performance") or {}
    for candidate, stats in list(shadow.items())[:6]:
        rows.append((f"shadow {str(candidate)[:12]}",
                     f"trades={stats.get('trades')} pnl={money(stats.get('realized'))}"))
    return rows
