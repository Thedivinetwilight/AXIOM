"""Forecast view models: Kronos payloads → display rows (Qt-free).

Every label here is explicit about provenance. Predicted values are always
called "Forecast …" and the panel carries a banner saying the section is model
output, never observed market data. Dispersion is labelled as sample
dispersion, never as a calibrated probability.

Functions take plain dicts (``KronosStatus.to_dict()`` / ``ForecastSnapshot
.to_dict()``) so the presentation logic stays testable without PySide6 or torch.
"""

from __future__ import annotations

FORECAST_BANNER = "MODEL FORECAST — not observed market data"

STATE_LABELS = {
    "DISABLED": "DISABLED (Kronos switched off)",
    "INITIALIZING": "INITIALIZING (preparing runtime)",
    "DOWNLOADING": "DOWNLOADING (fetching weights)",
    "LOADING": "LOADING (tokenizer/model)",
    "READY": "READY",
    "INFERENCING": "INFERENCING",
    "STALE": "STALE (forecast older than the staleness window)",
    "OFFLINE": "OFFLINE (Kronos process not running)",
    "ERROR": "ERROR",
    "INSUFFICIENT_HISTORY": "INSUFFICIENT_HISTORY (not enough closed candles)",
    "DATA_ERROR": "DATA_ERROR (market data rejected)",
}


def status_label(state: str | None) -> str:
    return STATE_LABELS.get(str(state or ""), str(state or "UNKNOWN"))


def data_quality_label(quality: dict | None) -> str:
    quality = quality or {}
    state = str(quality.get("state", "UNAVAILABLE"))
    if state == "OK":
        bars = quality.get("bars")
        return f"OK ({bars} closed bars)" if bars is not None else "OK"
    errors = quality.get("errors") or []
    detail = "; ".join(str(e) for e in errors[:2])
    return f"{state}" + (f" — {detail}" if detail else "")


def _money(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.4f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _stamp(value) -> str:
    if not value:
        return "—"
    return str(value)[:19].replace("T", " ")


def forecast_panel_rows(
    status: dict | None, snapshot: dict | None = None
) -> list[tuple[str, str]]:
    """FORECAST panel: runtime + latest forecast, with explicit provenance."""
    status = status or {}
    snapshot = snapshot or {}
    rows: list[tuple[str, str]] = [
        ("Provenance", FORECAST_BANNER),
        ("Status", status_label(status.get("state"))),
        ("Detail", str(status.get("message") or "—")),
        ("Model", str(status.get("model_id") or snapshot.get("model_id") or "—")),
        ("Model revision",
         str(status.get("model_revision") or snapshot.get("model_revision") or "not pinned")),
        ("Tokenizer", str(status.get("tokenizer_id") or snapshot.get("tokenizer_id") or "—")),
        ("Tokenizer revision",
         str(status.get("tokenizer_revision") or snapshot.get("tokenizer_revision")
             or "not pinned")),
        ("Backend", str(snapshot.get("backend") or status.get("backend") or "—")),
        ("Device", str(status.get("device") or snapshot.get("device") or "—")),
        ("Kronos runtime", str(status.get("kronos_source") or "—")),
        ("Model cache", str(status.get("cache_dir") or "—")),
        ("Worker pid", str(status.get("pid") or "—")),
    ]
    if not snapshot:
        rows.append(("Forecast", "none yet"))
        if status.get("last_error"):
            rows.append(("Last error", str(status["last_error"])))
        return rows

    uncertainty = snapshot.get("uncertainty") or {}
    rows += [
        ("Instrument", str(snapshot.get("instrument") or "—")),
        ("Timeframe", str(snapshot.get("timeframe") or "—")),
        ("Lookback", str(snapshot.get("lookback") or "—")),
        ("Prediction horizon",
         f"{snapshot.get('pred_len')} bars (max context {snapshot.get('max_context')})"),
        ("Sample paths", str(snapshot.get("sample_paths") or 1)),
        ("Sampling", f"T={snapshot.get('temperature')} top_p={snapshot.get('top_p')} "
                     f"top_k={snapshot.get('top_k')}"),
        ("Last closed candle (ACTUAL)", _stamp(snapshot.get("last_closed_timestamp"))),
        ("Last actual close (ACTUAL)", _money(snapshot.get("last_actual_close"))),
        ("Forecast endpoint (MODEL)", _stamp(snapshot.get("forecast_endpoint"))),
        ("Forecast close at horizon (MODEL)", _money(snapshot.get("horizon_close"))),
        ("Forecast horizon return (MODEL)", _pct(snapshot.get("horizon_return"))),
    ]
    if uncertainty.get("supported"):
        rows += [
            ("Forecast close P10 (MODEL)", _money(uncertainty.get("p10_close"))),
            ("Forecast close P50 (MODEL)", _money(uncertainty.get("p50_close"))),
            ("Forecast close P90 (MODEL)", _money(uncertainty.get("p90_close"))),
            ("Interval width P90−P10 (MODEL)",
             f"{_money(uncertainty.get('interval_width_close'))} "
             f"({_pct(uncertainty.get('interval_width_pct'))} of median)"),
            ("Dispersion (MODEL)", _money(uncertainty.get("dispersion_close"))),
            ("Uncertainty meaning", "sample dispersion (not calibrated probability)"),
            ("Sampled paths used", str(uncertainty.get("sample_count"))),
        ]
    else:
        rows.append(
            ("Uncertainty",
             "not available — needs multiple sampled trajectories "
             f"(sample_paths >= 3, configured {snapshot.get('sample_paths')})")
        )
    provenance = snapshot.get("provenance") or {}
    rows += [
        ("Inference latency", f"{snapshot.get('latency_ms', 0.0):.1f} ms"),
        ("Updated", _stamp(snapshot.get("generated_at"))),
        ("Input data source", str(provenance.get("source") or "—")),
        ("Input provider", str(provenance.get("provider") or "—")),
        ("Input window", f"{provenance.get('bars', 0)} closed bars "
                         f"({_stamp(provenance.get('first_timestamp'))} → "
                         f"{_stamp(provenance.get('last_timestamp'))})"),
        ("Input hash", str(snapshot.get("input_hash") or "—")[:16]),
        ("Cache key", str(snapshot.get("cache_key") or "—")[:16]),
        ("Data quality (input)", data_quality_label(snapshot.get("data_quality"))),
        ("Note", str(snapshot.get("note") or FORECAST_BANNER)),
    ]
    if status.get("last_error"):
        rows.append(("Last error", str(status["last_error"])))
    return rows


def forecast_table_rows(snapshots: list[dict] | None) -> list[list[str]]:
    """Compact multi-series table for the FORECAST page."""
    header = [
        "instrument", "tf", "status/quality", "last actual close (ACTUAL)",
        "forecast close (MODEL)", "horizon return (MODEL)", "endpoint (MODEL)",
        "latency ms", "updated",
    ]
    rows: list[list[str]] = [header]
    for snapshot in snapshots or []:
        rows.append([
            str(snapshot.get("instrument", "—")),
            str(snapshot.get("timeframe", "—")),
            data_quality_label(snapshot.get("data_quality")),
            _money(snapshot.get("last_actual_close")),
            _money(snapshot.get("horizon_close")),
            _pct(snapshot.get("horizon_return")),
            _stamp(snapshot.get("forecast_endpoint")),
            f"{snapshot.get('latency_ms', 0.0):.1f}",
            _stamp(snapshot.get("generated_at")),
        ])
    return rows


def overlay_series(overlay: dict | None) -> dict[str, list[tuple[str, float]]]:
    """Chart series for the MARKETS overlay, with unambiguous key names."""
    overlay = overlay or {}
    return {
        "ACTUAL close": list(overlay.get("actual_close") or []),
        "MODEL FORECAST close": list(overlay.get("forecast_close") or []),
        "MODEL FORECAST P10": list(overlay.get("forecast_close_p10") or []),
        "MODEL FORECAST P90": list(overlay.get("forecast_close_p90") or []),
    }


def overlay_legend() -> str:
    return ("solid = ACTUAL market data · dashed = MODEL FORECAST "
            "(predicted values are not observations)")


def forecast_candle_rows(snapshot: dict | None, limit: int = 12) -> list[list[str]]:
    """First/last few forecast bars, always labelled as forecast values."""
    header = ["timestamp", "forecast open", "forecast high", "forecast low",
              "forecast close", "forecast volume"]
    rows: list[list[str]] = [header]
    candles = (snapshot or {}).get("candles") or []
    for candle in candles[:limit]:
        rows.append([
            _stamp(candle.get("timestamp")),
            _money(candle.get("forecast_open")),
            _money(candle.get("forecast_high")),
            _money(candle.get("forecast_low")),
            _money(candle.get("forecast_close")),
            _money(candle.get("forecast_volume")),
        ])
    return rows
