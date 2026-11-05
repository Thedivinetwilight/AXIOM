"""Deterministic, auditable combination of Kronos forecasts with PAPER strategies.

Enforces AXIOM's non-negotiable forecasting boundaries:
- Kronos is strictly advisory and NEVER initiates a standalone order without an
  enabled technical/ensemble rule signal.
- Missing, stale, timed-out, or invalid forecasts follow the explicit configured
  ``failure_policy`` (``FAIL_CLOSED_NO_ENTRY`` by default; ``EXPLICIT_NON_KRONOS_FALLBACK``
  only when explicitly selected by the operator and prominently labeled).
- Records structured per-symbol explanations including forecast timestamp, input
  window, artifact/model identity, confidence/uncertainty, latency, inference
  status, and whether the forecast was actually consumed by the strategy.
"""

from __future__ import annotations

from typing import Any

from app.data.models import Candle


def build_forecast_summary(
    kronos_outcome: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extract a concise, secret-free forecast record for decision traces & UI."""
    if not isinstance(kronos_outcome, dict):
        return {
            "inference_status": "NOT_RUN",
            "forecast_produced": False,
            "generated_at": None,
            "input_window": None,
            "model_identity": None,
            "up_probability": None,
            "expected_return_pct": None,
            "q10_close": None,
            "mean_close": None,
            "q90_close": None,
            "directional_confidence": None,
            "effective_confidence": None,
            "uncertainty_pct": None,
            "freshness_state": None,
            "latency_ms": None,
            "failure_code": None,
            "failure_reason": None,
        }

    fc = kronos_outcome.get("forecast")
    if not isinstance(fc, dict):
        return {
            "inference_status": str(kronos_outcome.get("status") or "UNAVAILABLE"),
            "lifecycle_state": str(kronos_outcome.get("lifecycle_state") or "FAILED"),
            "forecast_produced": False,
            "generated_at": None,
            "input_window": None,
            "model_identity": None,
            "up_probability": None,
            "expected_return_pct": None,
            "q10_close": None,
            "mean_close": None,
            "q90_close": None,
            "directional_confidence": None,
            "effective_confidence": None,
            "uncertainty_pct": None,
            "freshness_state": None,
            "latency_ms": kronos_outcome.get("latency_ms"),
            "failure_code": kronos_outcome.get("error_code"),
            "failure_reason": kronos_outcome.get("error_reason"),
        }

    ident = fc.get("model_identity") or {}
    up_p = float(fc.get("up_probability") or 0.0)
    dir_conf = float(fc.get("directional_confidence") if fc.get("directional_confidence") is not None else max(up_p, 1.0 - up_p))
    eff_conf = float(fc.get("effective_confidence") if fc.get("effective_confidence") is not None else dir_conf)
    unc_pct = float(fc.get("uncertainty_band_q10_q90_pct") or 0.0)
    lat_obj = fc.get("latency_ms") or {}
    lat_ms = kronos_outcome.get("latency_ms")
    if lat_ms is None and isinstance(lat_obj, dict):
        lat_ms = lat_obj.get("inference_ms")

    exp_r_val = float(fc.get("expected_return_pct") or 0.0)
    dir_lbl = "UP" if (up_p > 0.5 and exp_r_val > 0) else ("DOWN" if (up_p < 0.5 and exp_r_val < 0) else "FLAT")
    return {
        "inference_status": str(kronos_outcome.get("status") or "OK"),
        "lifecycle_state": str(kronos_outcome.get("lifecycle_state") or "RUNNING"),
        "forecast_produced": True,
        "generated_at": fc.get("generated_at"),
        "direction": dir_lbl,
        "confidence": round(eff_conf, 4),
        "model_id": ident.get("model_repository", "NeoQuasar/Kronos-small"),
        "input_window": {
            "start": fc.get("input_start"),
            "end": fc.get("input_end"),
            "input_start": fc.get("input_start"),
            "input_end": fc.get("input_end"),
            "bars": fc.get("context_bars_used"),
            "bar_count": fc.get("context_bars_used"),
            "horizon_bars": fc.get("horizon_bars"),
            "timeframe": fc.get("timeframe"),
        },
        "model_identity": {
            "model_repository": ident.get("model_repository", "NeoQuasar/Kronos-small"),
            "model_revision": ident.get("model_revision", ""),
            "tokenizer_repository": ident.get("tokenizer_repository", "NeoQuasar/Kronos-Tokenizer-base"),
            "tokenizer_revision": ident.get("tokenizer_revision", ""),
            "manifest_sha256": ident.get("manifest_sha256", ""),
            "device": ident.get("device", "cpu"),
        },
        "last_close": fc.get("last_close"),
        "q10_close": fc.get("q10_close"),
        "mean_close": fc.get("mean_close"),
        "q90_close": fc.get("q90_close"),
        "up_probability": round(up_p, 4),
        "expected_return_pct": fc.get("expected_return_pct"),
        "directional_confidence": round(dir_conf, 4),
        "effective_confidence": round(eff_conf, 4),
        "uncertainty_pct": round(unc_pct, 4),
        "freshness_state": fc.get("freshness_state", "FRESH"),
        "latency_ms": lat_ms,
        "failure_code": None,
        "failure_reason": None,
    }


def combine_paper_strategy_decision(
    *,
    strategy: str,
    bars: list[Candle],
    base_rule_signal: dict[str, Any],
    kronos_configured: bool,
    kronos_outcome: dict[str, Any] | None,
    policy: dict[str, Any] | None = None,
    current_position_qty: float = 0.0,
) -> dict[str, Any]:
    """Combine technical rule signal with optional validated Kronos forecast."""
    strat = (strategy or "manual_only").strip().lower()
    bar_ts = bars[-1].timestamp.isoformat() if bars else None
    pol = dict(policy or {})
    weight_cap = min(0.25, max(0.0, float(pol.get("weight_cap", 0.20))))
    min_conf = min(0.95, max(0.50, float(pol.get("min_confidence", 0.55))))
    max_unc = max(0.5, float(pol.get("max_uncertainty_pct", 8.0)))
    conflict_policy = str(pol.get("conflict_policy") or "VETO_CONFLICTING_ENTRY")
    failure_policy = str(pol.get("failure_policy") or "FAIL_CLOSED_NO_ENTRY")

    fc_summary = build_forecast_summary(kronos_outcome)
    rule_action = str(base_rule_signal.get("action") or "HOLD").upper()
    rule_reason = str(base_rule_signal.get("reason") or "")

    # For kronos_ensemble / kronos_informed, derive underlying SMA5/SMA20 rule vote explicitly
    if strat in {"kronos_ensemble", "kronos_informed"}:
        if len(bars) >= 20:
            closes = [float(b.close) for b in bars]
            fast_now = sum(closes[-5:]) / 5.0
            slow_now = sum(closes[-20:]) / 20.0
            if fast_now > slow_now:
                rule_action = "BUY"
                rule_reason = f"SMA5 ({fast_now:.2f}) > SMA20 ({slow_now:.2f})"
            elif fast_now < slow_now:
                rule_action = "SELL"
                rule_reason = f"SMA5 ({fast_now:.2f}) < SMA20 ({slow_now:.2f})"
            else:
                rule_action = "HOLD"
                rule_reason = f"SMA5 ({fast_now:.2f}) == SMA20 ({slow_now:.2f})"
        else:
            rule_action = "HOLD"
            rule_reason = f"need >=20 completed bars (have {len(bars)})"

    strategy_signals = {
        "primary_strategy": strat,
        "rule_action": rule_action,
        "rule_reason": rule_reason,
    }
    policy_used = {
        "weight_cap": weight_cap,
        "min_confidence": min_conf,
        "max_uncertainty_pct": max_unc,
        "conflict_policy": conflict_policy,
        "failure_policy": failure_policy,
    }

    if not kronos_configured:
        return {
            "action": rule_action,
            "reason": rule_reason,
            "bar_ts": bar_ts,
            "kronos_status": "NOT_CONFIGURED",
            "forecast_produced": False,
            "forecast_consumed": False,
            "policy_applied": "NONE_NON_KRONOS",
            "forecast_summary": fc_summary,
            "strategy_signals": strategy_signals,
            "contribution_policy": policy_used,
        }

    fc = (kronos_outcome or {}).get("forecast") if isinstance(kronos_outcome, dict) else None
    is_valid_fresh = (
        isinstance(fc, dict)
        and bool((kronos_outcome or {}).get("ok"))
        and not bool(fc.get("mock_outputs_used"))
        and fc.get("freshness_state") == "FRESH"
    )

    if not is_valid_fresh:
        err_status = fc_summary.get("inference_status") or "UNAVAILABLE"
        if isinstance(fc, dict) and fc.get("freshness_state") == "STALE":
            err_status = "STALE"
            fc_summary["inference_status"] = "STALE"
            fc_summary["failure_reason"] = "Forecast exceeded max_forecast_age_seconds or mismatched bar timestamp."
        err_detail = fc_summary.get("failure_reason") or "Kronos forecast unavailable"

        if rule_action == "SELL" and float(current_position_qty) > 0.0:
            return {
                "action": "SELL",
                "reason": (
                    f"[PROTECTIVE EXIT ALLOWED · Kronos {err_status}: {err_detail}] {rule_reason}"
                ),
                "bar_ts": bar_ts,
                "kronos_status": "FAIL_CLOSED_EXIT_ALLOWED",
                "forecast_produced": bool(isinstance(fc, dict)),
                "forecast_consumed": False,
                "policy_applied": "PROTECTIVE_OR_EXIT_SELL_ALLOWED",
                "forecast_summary": fc_summary,
                "strategy_signals": strategy_signals,
                "contribution_policy": policy_used,
            }

        if failure_policy == "EXPLICIT_NON_KRONOS_FALLBACK":
            return {
                "action": rule_action,
                "reason": f"[NON-KRONOS FALLBACK · Kronos {err_status}: {err_detail}] {rule_reason}",
                "bar_ts": bar_ts,
                "kronos_status": "FALLBACK_NON_KRONOS",
                "forecast_produced": bool(isinstance(fc, dict)),
                "forecast_consumed": False,
                "policy_applied": "EXPLICIT_NON_KRONOS_FALLBACK",
                "forecast_summary": fc_summary,
                "strategy_signals": strategy_signals,
                "contribution_policy": policy_used,
            }

        return {
            "action": "HOLD",
            "reason": f"Kronos forecast unavailable or stale ({err_status}: {err_detail}); fail-closed no trade",
            "bar_ts": bar_ts,
            "kronos_status": "FAIL_CLOSED_BLOCKED",
            "forecast_produced": bool(isinstance(fc, dict)),
            "forecast_consumed": False,
            "policy_applied": "FAIL_CLOSED_NO_ENTRY",
            "forecast_summary": fc_summary,
            "strategy_signals": strategy_signals,
            "contribution_policy": policy_used,
        }

    # Valid, fresh Kronos forecast is available!
    up_p = float(fc.get("up_probability") or 0.0)
    exp_r = float(fc.get("expected_return_pct") or 0.0)
    unc_pct = float(fc.get("uncertainty_band_q10_q90_pct") or 0.0)
    dir_conf = float(fc.get("directional_confidence") if fc.get("directional_confidence") is not None else max(up_p, 1.0 - up_p))
    eff_conf = float(fc.get("effective_confidence") if fc.get("effective_confidence") is not None else dir_conf)
    sha_tag = str(((fc.get("model_identity") or {}).get("manifest_sha256") or ""))[:10]

    # 1. Wide uncertainty gate
    if unc_pct > max_unc:
        if failure_policy == "EXPLICIT_NON_KRONOS_FALLBACK" and strat not in {"kronos_ensemble", "kronos_informed"}:
            return {
                "action": rule_action,
                "reason": (
                    f"[Kronos band {unc_pct:.2f}% > max {max_unc:.2f}% · unconsumed] {rule_reason}"
                ),
                "bar_ts": bar_ts,
                "kronos_status": "INELIGIBLE_WIDE_UNCERTAINTY",
                "forecast_produced": True,
                "forecast_consumed": False,
                "policy_applied": "UNCERTAINTY_CAP_SKIPPED",
                "forecast_summary": fc_summary,
                "strategy_signals": strategy_signals,
                "contribution_policy": policy_used,
            }
        return {
            "action": "HOLD",
            "reason": (
                f"Kronos uncertainty band {unc_pct:.2f}% exceeds max {max_unc:.2f}%; fail-closed HOLD [sha:{sha_tag}]"
            ),
            "bar_ts": bar_ts,
            "kronos_status": "INELIGIBLE_WIDE_UNCERTAINTY",
            "forecast_produced": True,
            "forecast_consumed": False,
            "policy_applied": "UNCERTAINTY_CAP_HOLD",
            "forecast_summary": fc_summary,
            "strategy_signals": strategy_signals,
            "contribution_policy": policy_used,
        }

    # 2. Directional view under confidence threshold
    if up_p >= min_conf and exp_r > 0.0:
        kronos_direction = "BUY"
    elif (1.0 - up_p) >= min_conf and exp_r < 0.0:
        kronos_direction = "SELL"
    else:
        kronos_direction = "NEUTRAL"
    strategy_signals["kronos_direction"] = kronos_direction

    # 3. kronos_ensemble / kronos_informed: requires SMA trend + Kronos agreement
    if strat in {"kronos_ensemble", "kronos_informed"}:
        if len(bars) < 20:
            return {
                "action": "HOLD",
                "reason": rule_reason,
                "bar_ts": bar_ts,
                "kronos_status": "PRODUCED_INSUFFICIENT_RULE_BARS",
                "forecast_produced": True,
                "forecast_consumed": False,
                "policy_applied": "RULE_WARMUP_HOLD",
                "forecast_summary": fc_summary,
                "strategy_signals": strategy_signals,
                "contribution_policy": policy_used,
            }
        if rule_action == "BUY" and kronos_direction == "BUY":
            return {
                "action": "BUY",
                "reason": (
                    f"Kronos ensemble BUY: {rule_reason}, P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}%, "
                    f"conf={eff_conf:.2f}, band={unc_pct:.2f}%, w<={weight_cap:.2f} [sha:{sha_tag}]"
                ),
                "bar_ts": bar_ts,
                "kronos_status": "CONSUMED_CONFIRMED",
                "forecast_produced": True,
                "forecast_consumed": True,
                "policy_applied": "KRONOS_CONFIRMED_ENTRY",
                "forecast_summary": fc_summary,
                "strategy_signals": strategy_signals,
                "contribution_policy": policy_used,
            }
        if rule_action == "SELL" and kronos_direction == "SELL":
            return {
                "action": "SELL",
                "reason": (
                    f"Kronos ensemble SELL: {rule_reason}, P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}%, "
                    f"conf={eff_conf:.2f}, band={unc_pct:.2f}%, w<={weight_cap:.2f} [sha:{sha_tag}]"
                ),
                "bar_ts": bar_ts,
                "kronos_status": "CONSUMED_CONFIRMED",
                "forecast_produced": True,
                "forecast_consumed": True,
                "policy_applied": "KRONOS_CONFIRMED_ENTRY",
                "forecast_summary": fc_summary,
                "strategy_signals": strategy_signals,
                "contribution_policy": policy_used,
            }
        if rule_action in {"BUY", "SELL"} and kronos_direction in {"BUY", "SELL"} and rule_action != kronos_direction:
            return {
                "action": "HOLD",
                "reason": (
                    f"Kronos conflict veto: rule={rule_action} ({rule_reason}) opposed by Kronos={kronos_direction} "
                    f"(P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}%) [sha:{sha_tag}]"
                ),
                "bar_ts": bar_ts,
                "kronos_status": "CONSUMED_CONFLICT_VETO",
                "forecast_produced": True,
                "forecast_consumed": True,
                "policy_applied": conflict_policy,
                "forecast_summary": fc_summary,
                "strategy_signals": strategy_signals,
                "contribution_policy": policy_used,
            }
        return {
            "action": "HOLD",
            "reason": (
                f"Kronos advisory neutral/below threshold (min_conf={min_conf:.2f}): "
                f"P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}% [sha:{sha_tag}]"
            ),
            "bar_ts": bar_ts,
            "kronos_status": "CONSUMED_NEUTRAL_HOLD",
            "forecast_produced": True,
            "forecast_consumed": True,
            "policy_applied": "CONFIDENCE_THRESHOLD_HOLD",
            "forecast_summary": fc_summary,
            "strategy_signals": strategy_signals,
            "contribution_policy": policy_used,
        }

    # 4. Other technical rule strategies with Kronos advisory enabled for the PAPER session
    if rule_action == "HOLD":
        # Kronos NEVER initiates a solo order when the technical strategy is HOLD.
        return {
            "action": "HOLD",
            "reason": f"{rule_reason} (Kronos forecast produced={kronos_direction}, solo trade forbidden)",
            "bar_ts": bar_ts,
            "kronos_status": "PRODUCED_RULE_HOLD",
            "forecast_produced": True,
            "forecast_consumed": False,
            "policy_applied": "NO_SOLO_KRONOS_TRADE",
            "forecast_summary": fc_summary,
            "strategy_signals": strategy_signals,
            "contribution_policy": policy_used,
        }

    if kronos_direction == rule_action:
        return {
            "action": rule_action,
            "reason": (
                f"{rule_reason} · Kronos confirmed {kronos_direction} "
                f"(P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}%, w<={weight_cap:.2f}) [sha:{sha_tag}]"
            ),
            "bar_ts": bar_ts,
            "kronos_status": "CONSUMED_CONFIRMED",
            "forecast_produced": True,
            "forecast_consumed": True,
            "policy_applied": "KRONOS_CONFIRMED_ENTRY",
            "forecast_summary": fc_summary,
            "strategy_signals": strategy_signals,
            "contribution_policy": policy_used,
        }

    if kronos_direction in {"BUY", "SELL"} and kronos_direction != rule_action:
        if conflict_policy == "VETO_CONFLICTING_ENTRY":
            return {
                "action": "HOLD",
                "reason": (
                    f"Kronos conflict veto blocked {rule_action} ({rule_reason}): "
                    f"Kronos={kronos_direction} (P(up)={up_p:.2f}, E[ret]={exp_r:+.2f}%) [sha:{sha_tag}]"
                ),
                "bar_ts": bar_ts,
                "kronos_status": "CONSUMED_CONFLICT_VETO",
                "forecast_produced": True,
                "forecast_consumed": True,
                "policy_applied": "VETO_CONFLICTING_ENTRY",
                "forecast_summary": fc_summary,
                "strategy_signals": strategy_signals,
                "contribution_policy": policy_used,
            }
        return {
            "action": rule_action,
            "reason": (
                f"{rule_reason} · Kronos caution ({kronos_direction}, P(up)={up_p:.2f}; "
                f"conflict_policy=REDUCE_SCORE_ON_CONFLICT) [sha:{sha_tag}]"
            ),
            "bar_ts": bar_ts,
            "kronos_status": "CONSUMED_CONFLICT_CAUTION",
            "forecast_produced": True,
            "forecast_consumed": True,
            "policy_applied": "REDUCE_SCORE_ON_CONFLICT",
            "forecast_summary": fc_summary,
            "strategy_signals": strategy_signals,
            "contribution_policy": policy_used,
        }

    # kronos_direction == "NEUTRAL" while rule wants BUY/SELL
    if failure_policy == "FAIL_CLOSED_NO_ENTRY":
        return {
            "action": "HOLD",
            "reason": (
                f"Kronos directional confidence below {min_conf:.2f} (P(up)={up_p:.2f}) for {rule_action}; "
                f"fail-closed HOLD [sha:{sha_tag}]"
            ),
            "bar_ts": bar_ts,
            "kronos_status": "INELIGIBLE_LOW_CONFIDENCE",
            "forecast_produced": True,
            "forecast_consumed": False,
            "policy_applied": "LOW_CONFIDENCE_HOLD",
            "forecast_summary": fc_summary,
            "strategy_signals": strategy_signals,
            "contribution_policy": policy_used,
        }

    return {
        "action": rule_action,
        "reason": f"{rule_reason} · Kronos neutral (P(up)={up_p:.2f} < {min_conf:.2f}; rule fallback)",
        "bar_ts": bar_ts,
        "kronos_status": "INELIGIBLE_LOW_CONFIDENCE",
        "forecast_produced": True,
        "forecast_consumed": False,
        "policy_applied": "EXPLICIT_NON_KRONOS_FALLBACK",
        "forecast_summary": fc_summary,
        "strategy_signals": strategy_signals,
        "contribution_policy": policy_used,
    }
