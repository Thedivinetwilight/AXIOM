"""Structured, secret-redacted, correlated observability for PAPER trading & Kronos.

Provides:
- Recursive secret/token/header redaction across dicts, lists, and free-text strings.
- Canonical event normalization across ``LiveMarketPaperEngine`` (SQLite hash-chained
  ``live_paper_audit``), ``PaperSimulator`` / ``Repository`` (``system_events``), and
  ``KronosForecastProvider`` lifecycle events.
- Explicit labeling separating simulated paper events & simulated P&L from any
  broker-confirmed read-only or live records.
- Deterministic multi-field filtering by time range/window, market, symbol, event
  category/type, severity, strategy, session ID, and correlation/free-text search.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "api_token",
    "auth_token",
    "id_token",
    "bearer",
    "api_key",
    "apikey",
    "private_key",
    "client_secret",
    "authorization",
    "cookie",
    "set-cookie",
    "ciphertext",
    "apca-api-key-id",
    "apca-api-secret-key",
    "x-api-key",
)

EXACT_SENSITIVE_KEYS: frozenset[str] = frozenset({"token", "key", "pass", "pwd", "jwt"})

_INLINE_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b"), "[REDACTED_JWT]"),
    (
        re.compile(
            r"(?i)([?&](?:token|access_token|refresh_token|api_token|api_key|api_secret|client_secret|password|secret)=)([^&\s\"']+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(access_token|refresh_token|api_token|api_key|api_secret|client_secret|password|authorization|apca-api-secret-key|apca-api-key-id)\s*([:=])\s*([^\s,;\"'&]+)"
        ),
        r"\1\2[REDACTED]",
    ),
)

EVENT_CATEGORIES: tuple[str, ...] = (
    "KRONOS_LIFECYCLE",
    "STRATEGY_DECISION",
    "PAPER_ORDER",
    "SIMULATED_FILL",
    "LIVE_ORDER",
    "LIVE_FILL",
    "ORDER_REJECTION",
    "POSITION_CHANGE",
    "RISK_LIMIT",
    "SESSION_LIFECYCLE",
    "EMERGENCY_STOP",
    "AUDIT_OTHER",
)

SEVERITY_LEVELS: tuple[str, ...] = ("INFO", "WARN", "ERROR")


def _is_sensitive_key(key: Any) -> bool:
    norm = str(key or "").strip().lower().replace("-", "_")
    if not norm:
        return False
    if norm in EXACT_SENSITIVE_KEYS:
        return True
    if norm in {"instrument_key", "idempotency_key", "session_key", "cache_key", "sort_key", "public_key_id"}:
        return False
    return any(sub.replace("-", "_") in norm for sub in SENSITIVE_KEY_SUBSTRINGS)


def redact_sensitive_text(text: str) -> str:
    """Scrub inline tokens, Authorization headers, JWTs, and key=secret pairs."""
    cleaned = str(text)
    for pattern, repl in _INLINE_SECRET_PATTERNS:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned


def redact_sensitive_data(value: Any, *, _depth: int = 0) -> Any:
    """Recursively scrub secrets, tokens, keys, passwords, and sensitive headers."""
    if _depth > 12:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            k_str = str(k)
            if _is_sensitive_key(k_str):
                out[k_str] = "[REDACTED]"
            else:
                out[k_str] = redact_sensitive_data(v, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_data(item, _depth=_depth + 1) for item in value]
    return redact_sensitive_text(str(value))


def _parse_iso(ts: Any) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    raw = str(ts).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def classify_event_category_and_severity(event_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Map an event type + payload to canonical ``(category, severity)``."""
    et = str(event_type or "EVENT").strip().upper()
    explicit_sev = str(payload.get("severity") or "").strip().upper()

    if et.startswith("KRONOS_"):
        cat = "KRONOS_LIFECYCLE"
        if any(tok in et for tok in ("FAILED", "ERROR", "INVALID", "CRASH")):
            sev = "ERROR"
        elif any(tok in et for tok in ("TIMEOUT", "DEGRADED", "STALE", "FALLBACK")):
            sev = "WARN"
        else:
            sev = "INFO"
    elif et in {"PAPER_EMERGENCY_STOP", "LIVE_EMERGENCY_STOP", "EMERGENCY_STOP"}:
        cat, sev = "EMERGENCY_STOP", "ERROR"
    elif et in {"PAPER_EMERGENCY_STOP_CLEARED", "LIVE_EMERGENCY_STOP_CLEARED"}:
        cat, sev = "EMERGENCY_STOP", "WARN"
    elif et in {"PAPER_RISK_LIMIT_EVENT", "LIVE_RISK_REJECTED", "RISK_REJECTION", "RISK_REJECTED"}:
        cat, sev = "RISK_LIMIT", "WARN"
    elif et in {"PAPER_ORDER_REJECTED", "LIVE_ORDER_REJECTED", "ORDER_REJECTED"}:
        cat, sev = "ORDER_REJECTION", "WARN"
    elif et in {"LIVE_ORDER_AMBIGUOUS", "LIVE_ORDER_TIMEOUT"}:
        cat, sev = "LIVE_ORDER", "ERROR"
    elif et in {"LIVE_ORDER_FILLED", "LIVE_ORDER_PARTIAL_FILL"}:
        cat, sev = "LIVE_FILL", "INFO"
    elif et in {
        "LIVE_ORDER_INTENT",
        "LIVE_ORDER_SUBMITTED",
        "LIVE_ORDER_CANCELED",
        "LIVE_RECONCILED",
    }:
        cat, sev = "LIVE_ORDER", "INFO"
    elif et in {"PAPER_ORDER_FILLED", "PAPER_ORDER_FILL", "FILL"}:
        cat, sev = "SIMULATED_FILL", "INFO"
    elif et in {
        "PAPER_ORDER_SUBMITTED",
        "PAPER_ORDER_ACCEPTED",
        "PAPER_ORDER_WORKING",
        "PAPER_ORDER_CANCELED",
        "PAPER_ORDER_REPLACED",
        "ORDER_PLACED",
    }:
        cat, sev = "PAPER_ORDER", "INFO"
    elif et in {
        "PAPER_POSITION_CHANGED",
        "PAPER_POSITION_CLOSED",
        "PAPER_POSITION_PROTECTION_UPDATED",
        "LIVE_POSITION_CHANGED",
        "POSITION_OPENED",
        "POSITION_CLOSED",
    }:
        cat, sev = "POSITION_CHANGE", "INFO"
    elif et in {
        "PAPER_STRATEGY_DECISION",
        "LIVE_STRATEGY_DECISION",
        "DECISION_RECORDED",
        "PAPER_MARKET_ANALYSIS_REPORT",
        "LIVE_MARKET_ANALYSIS_REPORT",
        "PAPER_SYMBOL_SKIPPED",
        "LIVE_SYMBOL_SKIPPED",
    }:
        cat = "STRATEGY_DECISION"
        k_st = str(payload.get("kronos_status") or "")
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        if et in {"PAPER_SYMBOL_SKIPPED", "LIVE_SYMBOL_SKIPPED"}:
            sev = "ERROR" if str(payload.get("status") or "").upper() == "FAILED" else "WARN"
        elif et in {"PAPER_MARKET_ANALYSIS_REPORT", "LIVE_MARKET_ANALYSIS_REPORT"}:
            if int(counts.get("failed") or 0) > 0:
                sev = "ERROR"
            elif int(counts.get("missing_data") or 0) > 0 or int(counts.get("stale_data") or 0) > 0:
                sev = "WARN"
            else:
                sev = "INFO"
        elif "FAIL_CLOSED" in k_st:
            sev = "WARN"
        elif "FALLBACK" in k_st or "VETO" in k_st:
            sev = "WARN"
        else:
            sev = "INFO"
    elif any(
        tok in et
        for tok in (
            "PAPER_SESSION_",
            "LIVE_SESSION_",
            "LIVE_GATE_",
            "PAPER_SELECTED_MARKETS_",
            "PAPER_ACCOUNT_RESET",
            "SESSION_START",
            "SESSION_STOP",
            "SESSION_PAUSE",
            "SESSION_RESUME",
        )
    ):
        cat = "SESSION_LIFECYCLE"
        sev = "WARN" if "RECOVERED" in et else "INFO"
    else:
        cat = "AUDIT_OTHER"
        sev = "INFO"

    if explicit_sev in SEVERITY_LEVELS:
        sev = explicit_sev
    return cat, sev


def _build_summary(event_type: str, market: str, symbol: str, strategy: str, payload: dict[str, Any]) -> str:
    et = str(event_type or "EVENT")
    sym_part = f" {market}:{symbol}" if symbol else (f" {market}" if market else "")
    strat_part = f" [{strategy}]" if strategy else ""
    reason = str(
        payload.get("summary")
        or payload.get("reason")
        or payload.get("reject_reason")
        or payload.get("detail")
        or payload.get("failure_reason")
        or ""
    ).strip()
    if et in {"LIVE_ORDER_FILLED", "LIVE_ORDER_PARTIAL_FILL"}:
        side = payload.get("side", "")
        qty = payload.get("filled_qty") or payload.get("fill_qty") or payload.get("qty") or ""
        px = payload.get("filled_avg_price") or payload.get("fill_price") or ""
        broker = payload.get("broker") or ("Upstox" if market == "NSE" else "Alpaca")
        return f"LIVE BROKER FILL ({broker}){sym_part}{strat_part}: {side} {qty} @ {px}".strip()
    if et in {"LIVE_ORDER_SUBMITTED", "LIVE_ORDER_INTENT"}:
        side = payload.get("side", "")
        qty = payload.get("qty") or payload.get("quantity") or ""
        st = payload.get("status", "SUBMITTED")
        broker = payload.get("broker") or ("Upstox" if market == "NSE" else "Alpaca")
        return f"LIVE BROKER ORDER ({broker}){sym_part}{strat_part}: {side} {qty} -> {st}".strip()
    if et in {"LIVE_ORDER_AMBIGUOUS", "LIVE_ORDER_TIMEOUT"}:
        broker = payload.get("broker") or ("Upstox" if market == "NSE" else "Alpaca")
        return f"LIVE AMBIGUOUS SUBMISSION ({broker}){sym_part}: {reason or 'reconciliation required before retry'}".strip()
    if et in {"LIVE_ORDER_REJECTED", "LIVE_RISK_REJECTED"}:
        reasons = payload.get("reasons") or payload.get("violated_limits")
        if isinstance(reasons, list) and reasons:
            reason = "; ".join(str(x) for x in reasons)
        return f"{et}{sym_part}{strat_part}: {reason or 'blocked by central live risk gate'}".strip()
    if et in {"PAPER_ORDER_FILLED", "PAPER_ORDER_FILL"}:
        side = payload.get("side", "")
        qty = payload.get("fill_qty") or payload.get("qty") or ""
        px = payload.get("fill_price") or ""
        return f"SIMULATED FILL{sym_part}{strat_part}: {side} {qty} @ {px} (0 broker orders)".strip()
    if et in {"PAPER_ORDER_SUBMITTED", "PAPER_ORDER_ACCEPTED"}:
        side = payload.get("side", "")
        qty = payload.get("qty") or payload.get("quantity") or ""
        st = payload.get("status", "ACCEPTED")
        return f"SIMULATED ORDER{sym_part}{strat_part}: {side} {qty} -> {st}".strip()
    if et in {"PAPER_ORDER_REJECTED", "PAPER_RISK_LIMIT_EVENT"}:
        reasons = payload.get("reasons") or payload.get("violated_limits")
        if isinstance(reasons, list) and reasons:
            reason = "; ".join(str(x) for x in reasons)
        return f"{et}{sym_part}{strat_part}: {reason or 'blocked by paper risk guardrail'}".strip()
    if et.startswith("KRONOS_"):
        lat = payload.get("latency_ms")
        lat_txt = f" ({lat} ms)" if lat is not None else ""
        st_txt = str(payload.get("lifecycle_state") or payload.get("status") or "").strip()
        st_part = f" state={st_txt}" if st_txt else ""
        return f"{et}{sym_part}{strat_part}:{st_part}{lat_txt} {reason}".strip()
    if reason:
        return f"{et}{sym_part}{strat_part}: {reason}".strip()
    return f"{et}{sym_part}{strat_part}".strip()


def normalize_structured_event(
    *,
    event_id: str,
    timestamp: str,
    event_type: str,
    market: str = "LOCAL",
    source: str = "LIVE_MARKET_PAPER",
    payload: dict[str, Any] | None = None,
    entry_hash: str | None = None,
    seq: int | None = None,
) -> dict[str, Any]:
    """Normalize any paper, simulator, or Kronos event into the canonical UI/API schema."""
    safe_payload = redact_sensitive_data(payload or {})
    if not isinstance(safe_payload, dict):
        safe_payload = {"detail": str(safe_payload)}

    mkt = str(safe_payload.get("market") or market or "LOCAL").upper()
    sym = str(safe_payload.get("symbol") or "").upper()
    strategy = str(safe_payload.get("strategy") or "").strip()
    session_id = str(
        safe_payload.get("session_run_id")
        or safe_payload.get("session_id")
        or safe_payload.get("run_id")
        or ""
    ).strip()
    decision_id = str(safe_payload.get("decision_id") or "").strip()
    order_id = str(
        safe_payload.get("client_order_id")
        or safe_payload.get("order_id")
        or ""
    ).strip()
    fill_id = str(safe_payload.get("fill_id") or "").strip()
    correlation_id = decision_id or order_id or fill_id or session_id

    simulated = bool(safe_payload.get("simulated", True))
    real_money = bool(safe_payload.get("real_money", False))
    if real_money:
        execution_class = "LIVE_BROKER_CONFIRMED"
        execution_badge = "LIVE BROKER · REAL MONEY"
        pnl_class = "BROKER_CONFIRMED_REAL_PNL"
    elif not simulated and str(safe_payload.get("read_only", "")).lower() == "true":
        execution_class = "BROKER_CONFIRMED_READ_ONLY"
        execution_badge = "READ-ONLY MARKET PROBE · $0 ORDERS"
        pnl_class = "NONE"
    else:
        execution_class = "SIMULATED_PAPER"
        execution_badge = "SIMULATED PAPER · 0 BROKER ORDERS"
        pnl_class = "SIMULATED_PAPER_PNL"

    category, severity = classify_event_category_and_severity(event_type, safe_payload)
    summary = redact_sensitive_text(_build_summary(event_type, mkt, sym, strategy, safe_payload))

    return {
        "event_id": str(event_id),
        "seq": seq,
        "timestamp": str(timestamp),
        "source": str(source),
        "market": mkt,
        "symbol": sym,
        "strategy": strategy,
        "session_id": session_id,
        "decision_id": decision_id,
        "order_id": order_id,
        "fill_id": fill_id,
        "correlation_id": correlation_id,
        "category": category,
        "event_type": str(event_type),
        "severity": severity,
        "simulated": simulated and not real_money,
        "real_money": real_money,
        "execution_class": execution_class,
        "execution_badge": execution_badge,
        "pnl_classification": pnl_class,
        "pnl_scope_label": pnl_class,
        "summary": summary,
        "entry_hash": entry_hash,
        "payload": safe_payload,
        "detail": safe_payload,
    }


def filter_structured_events(
    events: list[dict[str, Any]],
    *,
    now_utc: datetime | None = None,
    window: str = "",
    since: str = "",
    until: str = "",
    market: str = "",
    symbol: str = "",
    category: str = "",
    event_type: str = "",
    severity: str = "",
    strategy: str = "",
    session_id: str = "",
    query: str = "",
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Filter normalized structured events across all operator log dimensions."""
    ref_now = now_utc or datetime.now(UTC)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=UTC)

    since_dt = _parse_iso(since)
    until_dt = _parse_iso(until)
    win = str(window or "").strip().lower()
    if since_dt is None and win and win != "all":
        deltas = {
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "6h": timedelta(hours=6),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
        }
        if win in deltas:
            since_dt = ref_now - deltas[win]

    mkt_f = str(market or "").strip().upper()
    if mkt_f == "ALL":
        mkt_f = ""
    sym_f = str(symbol or "").strip().upper()
    cat_f = str(category or "").strip().upper()
    if cat_f == "ALL":
        cat_f = ""
    et_f = str(event_type or "").strip().upper()
    if et_f == "ALL":
        et_f = ""
    sev_f = str(severity or "").strip().upper()
    if sev_f == "ALL":
        sev_f = ""
    strat_f = str(strategy or "").strip().lower()
    if strat_f == "all":
        strat_f = ""
    sess_f = str(session_id or "").strip()
    if sess_f.upper() == "ALL":
        sess_f = ""
    q_f = str(query or "").strip().lower()

    matched: list[dict[str, Any]] = []
    for ev in events:
        if mkt_f and str(ev.get("market") or "").upper() != mkt_f:
            continue
        if sym_f and sym_f not in str(ev.get("symbol") or "").upper():
            continue
        if cat_f and str(ev.get("category") or "").upper() != cat_f:
            continue
        if et_f and et_f not in str(ev.get("event_type") or "").upper():
            continue
        if sev_f and str(ev.get("severity") or "").upper() != sev_f:
            continue
        if strat_f and strat_f not in str(ev.get("strategy") or "").lower():
            continue
        if sess_f and sess_f not in str(ev.get("session_id") or ""):
            continue
        if since_dt is not None or until_dt is not None:
            ev_dt = _parse_iso(ev.get("timestamp"))
            if ev_dt is None:
                continue
            if since_dt is not None and ev_dt < since_dt:
                continue
            if until_dt is not None and ev_dt > until_dt:
                continue
        if q_f:
            haystack = " ".join(
                (
                    str(ev.get("event_type") or ""),
                    str(ev.get("category") or ""),
                    str(ev.get("market") or ""),
                    str(ev.get("symbol") or ""),
                    str(ev.get("strategy") or ""),
                    str(ev.get("session_id") or ""),
                    str(ev.get("decision_id") or ""),
                    str(ev.get("order_id") or ""),
                    str(ev.get("summary") or ""),
                    str(ev.get("payload") or ""),
                )
            ).lower()
            if q_f not in haystack:
                continue
        matched.append(ev)

    matched.sort(key=lambda item: (str(item.get("timestamp") or ""), int(item.get("seq") or 0)), reverse=True)
    return matched[: max(1, min(int(limit), 2000))]
