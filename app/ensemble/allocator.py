"""Single Per-Market Portfolio Risk Allocator & Pre-Trade Risk Gate.

Combines candidate strategy signals and optional advisory Kronos forecasts into at
most ONE risk-bounded, lot/tick-compliant decision per (market, symbol, timeframe, bar).
Individual strategies never submit orders directly.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from app.data.models import parse_timestamp
from app.ensemble.calibration import EmpiricalCalibrator, classify_bar_regime
from app.ensemble.registry import SharedIndicatorContext, StrategySignalRecord
from app.markets.live_paper import _round_to_tick, estimate_market_fees


def pearson_return_correlation(rets_a: list[float], rets_b: list[float], *, min_overlap: int = 15) -> float | None:
    """Compute trailing Pearson correlation between two return series."""
    n = min(len(rets_a), len(rets_b))
    if n < min_overlap:
        return None
    xa = rets_a[-n:]
    xb = rets_b[-n:]
    ma = sum(xa) / n
    mb = sum(xb) / n
    va = sum((x - ma) ** 2 for x in xa)
    vb = sum((y - mb) ** 2 for y in xb)
    if va <= 1e-16 or vb <= 1e-16:
        return 0.0
    cov = sum((xa[i] - ma) * (xb[i] - mb) for i in range(n))
    return max(-1.0, min(1.0, cov / math.sqrt(va * vb)))


@dataclass
class PortfolioAllocationConfig:
    per_trade_risk_pct: float = 0.005  # 0.50% of equity per trade risk budget
    max_order_value_pct: float = 0.20  # 20% of equity cap (additionally capped by PaperMarketConfig.max_order_value)
    max_symbol_exposure_pct: float = 0.25
    max_total_exposure_pct: float = 0.80
    max_open_positions: int = 8
    max_daily_loss_pct: float = 0.025
    max_drawdown_pct: float = 0.10
    max_spread_bps: float = 35.0
    slippage_bps: float = 5.0
    brokerage_bps: float = 3.0
    min_bar_volume: float = 500.0
    max_bar_participation: float = 0.05
    max_position_correlation: float = 0.80
    max_correlated_cluster_pct: float = 0.35
    max_quote_age_seconds: float = 90.0
    min_calibrated_confidence: float = 0.53
    min_oos_samples: int = 20
    min_consensus_score: float = 0.12
    max_conflict_ratio: float = 0.38
    reward_to_risk_multiple: float = 2.0
    kronos_enabled: bool = False
    kronos_advisory_weight: float = 0.18  # Capped <= 0.25 so Kronos never overrides ensemble alone
    kronos_fail_closed_policy: str = "REQUIRE_KRONOS"  # REQUIRE_KRONOS | DEGRADE_TO_NON_KRONOS
    allow_short_selling: bool = False

    def validate(self) -> PortfolioAllocationConfig:
        self.per_trade_risk_pct = max(0.0005, min(0.05, float(self.per_trade_risk_pct)))
        self.max_order_value_pct = max(0.01, min(1.0, float(self.max_order_value_pct)))
        self.max_symbol_exposure_pct = max(0.02, min(1.0, float(self.max_symbol_exposure_pct)))
        self.max_total_exposure_pct = max(0.05, min(1.0, float(self.max_total_exposure_pct)))
        self.max_open_positions = max(1, min(100, int(self.max_open_positions)))
        self.max_daily_loss_pct = max(0.002, min(0.25, float(self.max_daily_loss_pct)))
        self.max_drawdown_pct = max(0.01, min(0.50, float(self.max_drawdown_pct)))
        self.max_spread_bps = max(1.0, min(500.0, float(self.max_spread_bps)))
        self.slippage_bps = max(0.0, min(250.0, float(self.slippage_bps)))
        self.brokerage_bps = max(0.0, min(100.0, float(self.brokerage_bps)))
        self.min_bar_volume = max(0.0, float(self.min_bar_volume))
        self.max_bar_participation = max(0.001, min(0.50, float(self.max_bar_participation)))
        self.max_position_correlation = max(0.20, min(0.99, float(self.max_position_correlation)))
        self.max_correlated_cluster_pct = max(0.05, min(1.0, float(self.max_correlated_cluster_pct)))
        self.max_quote_age_seconds = max(1.0, min(86400.0, float(self.max_quote_age_seconds)))
        self.min_calibrated_confidence = max(0.50, min(0.95, float(self.min_calibrated_confidence)))
        self.min_oos_samples = max(1, min(10000, int(self.min_oos_samples)))
        self.min_consensus_score = max(0.01, min(0.95, float(self.min_consensus_score)))
        self.max_conflict_ratio = max(0.05, min(0.50, float(self.max_conflict_ratio)))
        self.reward_to_risk_multiple = max(1.0, min(10.0, float(self.reward_to_risk_multiple)))
        self.kronos_advisory_weight = max(0.0, min(0.25, float(self.kronos_advisory_weight)))
        if self.kronos_fail_closed_policy not in {"REQUIRE_KRONOS", "DEGRADE_TO_NON_KRONOS"}:
            self.kronos_fail_closed_policy = "REQUIRE_KRONOS"
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_kronos_advisory(
    kronos_forecast: dict[str, Any] | None,
    *,
    kronos_enabled: bool,
    latest_bar_timestamp: str,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate optional Kronos forecast without ever fabricating fallback values."""
    if not kronos_enabled:
        return {
            "enabled": False,
            "usable": False,
            "status": "DISABLED",
            "reason": "Kronos advisory input is disabled for this allocator.",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": None,
        }

    if not isinstance(kronos_forecast, dict) or not kronos_forecast:
        return {
            "enabled": True,
            "usable": False,
            "status": "UNAVAILABLE",
            "reason": "Kronos forecast is unavailable (runtime/weights not verified or failed closed).",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": None,
        }

    if kronos_forecast.get("mock_outputs_used") is not False or kronos_forecast.get("advisory_only") is not True:
        return {
            "enabled": True,
            "usable": False,
            "status": "IDENTITY_MISMATCH",
            "reason": "Rejected Kronos payload: mock_outputs_used must be False and advisory_only must be True.",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": None,
        }

    ident = kronos_forecast.get("model_identity") or {}
    manifest_sha = str(ident.get("manifest_sha256") or "")
    if not manifest_sha or len(manifest_sha) < 16:
        return {
            "enabled": True,
            "usable": False,
            "status": "IDENTITY_MISMATCH",
            "reason": "Rejected Kronos payload: missing verified manifest_sha256.",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": None,
        }
    if expected_manifest_sha256 and manifest_sha != expected_manifest_sha256:
        return {
            "enabled": True,
            "usable": False,
            "status": "IDENTITY_MISMATCH",
            "reason": f"Kronos manifest digest {manifest_sha[:12]} != expected {expected_manifest_sha256[:12]}.",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": manifest_sha,
        }

    if str(kronos_forecast.get("freshness_state") or "").upper() != "FRESH":
        return {
            "enabled": True,
            "usable": False,
            "status": "STALE",
            "reason": f"Kronos forecast freshness_state is {kronos_forecast.get('freshness_state')!r} (must be FRESH).",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": manifest_sha,
        }

    fc_bar_ts = str(kronos_forecast.get("input_end") or "")
    try:
        if parse_timestamp(fc_bar_ts).astimezone(UTC) != parse_timestamp(latest_bar_timestamp).astimezone(UTC):
            return {
                "enabled": True,
                "usable": False,
                "status": "STALE",
                "reason": f"Kronos input_end ({fc_bar_ts}) does not match latest completed bar ({latest_bar_timestamp}).",
                "directional_score": 0.0,
                "expected_return_pct": None,
                "up_probability": None,
                "q10_close": None,
                "q90_close": None,
                "manifest_sha256": manifest_sha,
            }
    except (ValueError, TypeError):
        return {
            "enabled": True,
            "usable": False,
            "status": "MALFORMED",
            "reason": "Kronos forecast has unparseable input_end timestamp.",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": manifest_sha,
        }

    try:
        mean_c = float(kronos_forecast["mean_close"])
        q10_c = float(kronos_forecast["q10_close"])
        q90_c = float(kronos_forecast["q90_close"])
        up_p = float(kronos_forecast["up_probability"])
        exp_ret = float(kronos_forecast.get("expected_return_pct", 0.0))
    except (KeyError, TypeError, ValueError):
        return {
            "enabled": True,
            "usable": False,
            "status": "MALFORMED",
            "reason": "Kronos forecast is missing required numeric quantile/probability fields.",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": manifest_sha,
        }

    if (
        not all(math.isfinite(x) for x in (mean_c, q10_c, q90_c, up_p, exp_ret))
        or mean_c <= 0
        or q10_c > q90_c
        or not (0.0 <= up_p <= 1.0)
    ):
        return {
            "enabled": True,
            "usable": False,
            "status": "MALFORMED",
            "reason": "Kronos forecast failed numeric bounds validation.",
            "directional_score": 0.0,
            "expected_return_pct": None,
            "up_probability": None,
            "q10_close": None,
            "q90_close": None,
            "manifest_sha256": manifest_sha,
        }

    dir_score = max(-1.0, min(1.0, (up_p - 0.5) * 2.0))
    return {
        "enabled": True,
        "usable": True,
        "status": "VERIFIED_FRESH",
        "reason": "Verified CPU Kronos forecast aligned to latest completed bar.",
        "directional_score": round(dir_score, 5),
        "expected_return_pct": round(exp_ret, 4),
        "up_probability": round(up_p, 4),
        "q10_close": round(q10_c, 4),
        "q90_close": round(q90_c, 4),
        "manifest_sha256": manifest_sha,
    }


class PortfolioRiskAllocator:
    """Combines parallel strategy signals + optional advisory Kronos + calibrated OOS
    confidence + shared account-level risk limits into a single idempotent decision."""

    def __init__(self, config: PortfolioAllocationConfig | None = None) -> None:
        self.config = (config or PortfolioAllocationConfig()).validate()

    def allocate_symbol(
        self,
        *,
        market: str,
        symbol: str,
        timeframe: str,
        ctx: SharedIndicatorContext,
        signals: list[StrategySignalRecord],
        weights: dict[str, float],
        calibrator: EmpiricalCalibrator,
        account_state: dict[str, Any],
        positions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
        instrument_meta: dict[str, Any],
        quote: dict[str, Any] | None,
        session_is_open: bool,
        already_decided_for_bar: bool = False,
        returns_by_symbol: dict[str, list[float]] | None = None,
        kronos_forecast: dict[str, Any] | None = None,
        expected_kronos_manifest_sha256: str | None = None,
        hard_limits: dict[str, Any] | None = None,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Produce ONE final portfolio-bounded decision (`BUY`, `SELL`, or `NO_TRADE`)."""
        cfg = self.config
        mkt = market.strip().upper()
        sym = symbol.strip().upper()
        ref_now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        bar_ts = ctx.latest_bar.timestamp.astimezone(UTC).isoformat()
        regime = classify_bar_regime(ctx)

        # 1. Deduplicate canonical variants & aggregate OOS-weighted signals
        seen_sigs: set[str] = set()
        bull_w = 0.0
        bear_w = 0.0
        neutral_w = 0.0
        weighted_score_num = 0.0
        weighted_stop_num = 0.0
        active_w_sum = 0.0
        zero_weight_skipped = 0
        duplicate_skipped = 0
        active_families: set[str] = set()
        per_strategy_summary: list[dict[str, Any]] = []

        for sig in signals:
            if sig.status == "DUPLICATE_SKIPPED" or sig.canonical_signature in seen_sigs:
                duplicate_skipped += 1
                per_strategy_summary.append(
                    {
                        "strategy_id": sig.strategy_id,
                        "family": sig.family,
                        "action": sig.action,
                        "raw_score": sig.raw_score,
                        "weight": 0.0,
                        "contribution": 0.0,
                        "status": "DUPLICATE_DEDUPED",
                        "rationale": sig.rationale,
                    }
                )
                continue
            seen_sigs.add(sig.canonical_signature)
            w = max(0.0, float(weights.get(sig.strategy_id, 0.0)))
            if w <= 0.0:
                zero_weight_skipped += 1
                per_strategy_summary.append(
                    {
                        "strategy_id": sig.strategy_id,
                        "family": sig.family,
                        "action": sig.action,
                        "raw_score": sig.raw_score,
                        "weight": 0.0,
                        "contribution": 0.0,
                        "status": "REJECTED_ZERO_OOS_WEIGHT",
                        "rationale": sig.rationale,
                    }
                )
                continue

            active_w_sum += w
            active_families.add(sig.family)
            unit_signal = float(sig.direction) * 0.65 + float(sig.raw_score) * 0.35
            contrib = w * unit_signal
            weighted_score_num += contrib
            weighted_stop_num += w * sig.stop_distance_pct
            if sig.direction > 0:
                bull_w += w
            elif sig.direction < 0:
                bear_w += w
            else:
                neutral_w += w
            per_strategy_summary.append(
                {
                    "strategy_id": sig.strategy_id,
                    "family": sig.family,
                    "action": sig.action,
                    "raw_score": sig.raw_score,
                    "weight": round(w, 5),
                    "contribution": round(contrib, 5),
                    "status": "WEIGHTED_ACTIVE",
                    "rationale": sig.rationale,
                }
            )

        pre_kronos_consensus = (weighted_score_num / active_w_sum) if active_w_sum > 1e-12 else 0.0
        avg_stop_pct = (weighted_stop_num / active_w_sum) if active_w_sum > 1e-12 else max(0.6, (ctx.atr(14) / max(1e-6, ctx.last_close)) * 180.0)
        directional_w_sum = bull_w + bear_w
        conflict_ratio = (min(bull_w, bear_w) / directional_w_sum) if directional_w_sum > 1e-12 else 0.0

        # 2. Optional Advisory Kronos Inspection & Blending
        kronos_info = inspect_kronos_advisory(
            kronos_forecast,
            kronos_enabled=cfg.kronos_enabled,
            latest_bar_timestamp=bar_ts,
            expected_manifest_sha256=expected_kronos_manifest_sha256,
        )
        kronos_weight_applied = 0.0
        post_kronos_consensus = pre_kronos_consensus
        kronos_degraded = False
        kronos_policy_passed = True
        kronos_policy_detail = "Kronos disabled"

        if cfg.kronos_enabled:
            if kronos_info["usable"]:
                kronos_weight_applied = cfg.kronos_advisory_weight
                post_kronos_consensus = (
                    (1.0 - kronos_weight_applied) * pre_kronos_consensus
                    + kronos_weight_applied * float(kronos_info["directional_score"])
                )
                kronos_policy_detail = (
                    f"Advisory Kronos blended at weight {kronos_weight_applied:.2f} "
                    f"(score={kronos_info['directional_score']:+.3f})"
                )
            elif cfg.kronos_fail_closed_policy == "REQUIRE_KRONOS":
                kronos_policy_passed = False
                kronos_policy_detail = f"FAIL-CLOSED ({kronos_info['status']}): {kronos_info['reason']}"
            else:
                kronos_degraded = True
                kronos_policy_detail = f"Degraded to non-Kronos ensemble ({kronos_info['status']}): {kronos_info['reason']}"

        kronos_contribution = {
            **kronos_info,
            "policy": cfg.kronos_fail_closed_policy,
            "advisory_weight_applied": round(kronos_weight_applied, 4),
            "degraded_to_non_kronos": kronos_degraded,
            "pre_kronos_consensus": round(pre_kronos_consensus, 5),
            "post_kronos_consensus": round(post_kronos_consensus, 5),
            "consensus_delta": round(post_kronos_consensus - pre_kronos_consensus, 5),
        }

        # 3. Calibrated Out-of-Sample Confidence
        cal_est = calibrator.estimate(
            post_kronos_consensus,
            regime=regime,
            min_oos_samples=cfg.min_oos_samples,
        )
        cal_conf = cal_est["calibrated_confidence"]

        # Candidate direction before risk gates
        raw_intent = "HOLD"
        if post_kronos_consensus >= cfg.min_consensus_score:
            raw_intent = "BUY"
        elif post_kronos_consensus <= -cfg.min_consensus_score:
            raw_intent = "SELL"

        # 4. Shared Account-Level Risk Checks & Position/Order Reconciliation
        risk_checks: list[dict[str, Any]] = []
        no_trade_reasons: list[str] = []

        def add_check(check_id: str, passed: bool, actual: Any, limit: Any, detail: str) -> None:
            risk_checks.append(
                {
                    "check_id": check_id,
                    "passed": bool(passed),
                    "actual": actual,
                    "limit": limit,
                    "detail": detail,
                }
            )
            if not passed:
                no_trade_reasons.append(f"{check_id}: {detail}")

        # Check 1: Idempotency per (market, symbol, timeframe, bar_timestamp)
        add_check(
            "idempotency_bar_lock",
            not already_decided_for_bar,
            "ALREADY_EVALUATED" if already_decided_for_bar else "FIRST_EVALUATION",
            "SINGLE_DECISION_PER_BAR",
            "Bar already produced a persisted decision" if already_decided_for_bar else "Unique bar timestamp verified",
        )

        # Check 2: Market session open
        add_check(
            "market_session_open",
            bool(session_is_open),
            "OPEN" if session_is_open else "CLOSED",
            "OPEN",
            f"{mkt} regular session is {'OPEN' if session_is_open else 'CLOSED'}",
        )

        # Check 3: Kronos policy
        add_check(
            "kronos_policy_check",
            kronos_policy_passed,
            kronos_info["status"],
            cfg.kronos_fail_closed_policy if cfg.kronos_enabled else "DISABLED",
            kronos_policy_detail,
        )

        # Check 4: Quote freshness & spread
        quote_ok = False
        quote_age_s = None
        spread_bps = 0.0
        ref_price = ctx.last_close
        q_raw_last = quote.get("last") if isinstance(quote, dict) and quote.get("last") is not None else (quote.get("price") if isinstance(quote, dict) else None)
        if isinstance(quote, dict) and q_raw_last is not None and quote.get("timestamp"):
            try:
                q_last = float(q_raw_last)
                q_ts = parse_timestamp(str(quote["timestamp"])).astimezone(UTC)
                quote_age_s = round(max(0.0, (ref_now - q_ts).total_seconds()), 2)
                if math.isfinite(q_last) and q_last > 0 and quote_age_s <= cfg.max_quote_age_seconds:
                    quote_ok = True
                    ref_price = q_last
                bid = float(quote.get("bid") or 0.0)
                ask = float(quote.get("ask") or 0.0)
                if bid > 0 and ask >= bid:
                    mid = 0.5 * (bid + ask)
                    spread_bps = ((ask - bid) / mid) * 10_000.0 if mid > 0 else 0.0
                    ref_price = ask if raw_intent == "BUY" else (bid if raw_intent == "SELL" else q_last)
            except (TypeError, ValueError):
                quote_ok = False
        add_check(
            "quote_freshness",
            quote_ok,
            f"{quote_age_s}s" if quote_age_s is not None else "MISSING",
            f"<= {cfg.max_quote_age_seconds:.0f}s",
            "Fresh read-only quote verified" if quote_ok else "Quote missing or exceeded max_quote_age_seconds",
        )

        # Check 5: Active OOS strategy weights & consensus
        has_weights = active_w_sum > 1e-9
        add_check(
            "oos_strategy_weights",
            has_weights,
            round(active_w_sum, 4),
            "> 0.0",
            f"{len(active_families)} strategy families carry positive OOS weight"
            if has_weights
            else "All strategy variants have zero OOS walk-forward weight",
        )

        consensus_ok = raw_intent in {"BUY", "SELL"} and conflict_ratio <= cfg.max_conflict_ratio
        conflict_detail = (
            f"Consensus {post_kronos_consensus:+.3f} ({raw_intent}), conflict_ratio={conflict_ratio:.2f}"
            if raw_intent in {"BUY", "SELL"}
            else f"Neutral consensus {post_kronos_consensus:+.3f} within [-{cfg.min_consensus_score:.2f}, +{cfg.min_consensus_score:.2f}]"
        )
        if conflict_ratio > cfg.max_conflict_ratio:
            conflict_detail = f"Opposing strategy signals conflict ({conflict_ratio:.2f} > {cfg.max_conflict_ratio:.2f})"
        add_check(
            "signal_consensus",
            consensus_ok,
            round(post_kronos_consensus, 4),
            f"|score| >= {cfg.min_consensus_score:.2f}, conflict <= {cfg.max_conflict_ratio:.2f}",
            conflict_detail,
        )

        # Check 6: Calibrated confidence & minimum OOS sample evidence
        cal_ok = (
            cal_conf is not None
            and int(cal_est["oos_sample_size"]) >= cfg.min_oos_samples
            and float(cal_conf) >= cfg.min_calibrated_confidence
            and cal_est["calibration_quality"] != "INSUFFICIENT_EVIDENCE"
        )
        add_check(
            "calibrated_confidence_and_oos_evidence",
            cal_ok,
            {"confidence": cal_conf, "oos_samples": cal_est["oos_sample_size"]},
            {"min_confidence": cfg.min_calibrated_confidence, "min_oos_samples": cfg.min_oos_samples},
            f"Calibrated p={cal_conf} on N={cal_est['oos_sample_size']} OOS samples"
            if cal_ok
            else f"Insufficient calibrated confidence ({cal_conf}) or OOS samples ({cal_est['oos_sample_size']})",
        )

        # Check 7: Account daily loss & drawdown caps
        equity = max(1.0, float(account_state.get("equity") or account_state.get("cash") or 100_000.0))
        cash = max(0.0, float(account_state.get("cash") or equity))
        starting_cash = max(equity, float(account_state.get("starting_cash") or equity))
        peak_equity = max(starting_cash, float(account_state.get("peak_equity") or starting_cash))
        day_pnl = float(account_state.get("day_pnl") or 0.0)
        drawdown_pct = max(0.0, (peak_equity - equity) / peak_equity)

        hard = hard_limits or {}
        max_daily_loss_abs = min(
            equity * cfg.max_daily_loss_pct,
            float(hard.get("max_daily_loss", equity * cfg.max_daily_loss_pct)),
        )
        loss_ok = (-day_pnl) < max_daily_loss_abs and drawdown_pct < cfg.max_drawdown_pct
        add_check(
            "daily_loss_and_drawdown",
            loss_ok,
            {"day_pnl": round(day_pnl, 2), "drawdown_pct": round(drawdown_pct * 100.0, 2)},
            {"max_daily_loss": round(max_daily_loss_abs, 2), "max_drawdown_pct": round(cfg.max_drawdown_pct * 100.0, 2)},
            "Within daily loss and drawdown ceilings" if loss_ok else "Daily loss or max drawdown limit breached",
        )

        # Check 8: Existing positions & pending working orders reconciliation
        held_pos = next((p for p in positions if str(p.get("symbol", "")).upper() == sym and float(p.get("qty", 0)) > 0), None)
        held_qty = float(held_pos["qty"]) if held_pos else 0.0
        sym_working_orders = [
            o for o in open_orders
            if str(o.get("symbol", "")).upper() == sym
            and str(o.get("status", "")).upper() in {"WORKING", "PARTIALLY_FILLED", "PENDING_NEW", "ACCEPTED"}
        ]
        no_pending_on_sym = len(sym_working_orders) == 0
        pos_side_ok = True
        pos_detail = "No conflicting working order on symbol"
        if not no_pending_on_sym:
            pos_side_ok = False
            pos_detail = f"{len(sym_working_orders)} pending working order(s) already active on {sym}"
        elif raw_intent == "SELL" and held_qty <= 0 and not cfg.allow_short_selling:
            pos_side_ok = False
            pos_detail = f"SELL signal ignored in cash/delivery mode because held position on {sym} is 0"
        add_check(
            "existing_orders_and_positions",
            pos_side_ok,
            {"held_qty": held_qty, "working_orders_on_symbol": len(sym_working_orders)},
            "0 working orders; long held for cash SELL",
            pos_detail,
        )

        # Check 9: Liquidity, volume & bid-ask spread
        last_vol = float(ctx.latest_bar.volume)
        liq_ok = last_vol >= cfg.min_bar_volume and spread_bps <= cfg.max_spread_bps
        add_check(
            "liquidity_and_spread",
            liq_ok,
            {"bar_volume": last_vol, "spread_bps": round(spread_bps, 2)},
            {"min_bar_volume": cfg.min_bar_volume, "max_spread_bps": cfg.max_spread_bps},
            "Bar volume and spread within bounds" if liq_ok else "Insufficient bar volume or excessive bid-ask spread",
        )

        # Portfolio exposure accounting (existing positions + ALL pending working BUY orders)
        open_pos_list = [p for p in positions if float(p.get("qty", 0)) > 0]
        total_pos_exposure = sum(
            float(p.get("qty", 0)) * float(p.get("last_price") or p.get("avg_entry_price") or p.get("avg_price") or 0.0)
            for p in open_pos_list
        )
        pending_buy_exposure = sum(
            max(0.0, float(o.get("qty", 0)) - float(o.get("filled_qty", 0)))
            * float(o.get("limit_price") or o.get("expected_price") or ref_price)
            for o in open_orders
            if str(o.get("side", "")).upper() == "BUY"
        )
        current_total_exposure = total_pos_exposure + pending_buy_exposure
        current_sym_exposure = held_qty * ref_price

        # Check 10: Open position count limit (for new BUY entries)
        max_pos_cap = min(cfg.max_open_positions, int(hard.get("max_open_positions", cfg.max_open_positions)))
        pos_count_ok = True
        if raw_intent == "BUY" and held_qty <= 0 and len(open_pos_list) >= max_pos_cap:
            pos_count_ok = False
        add_check(
            "open_position_limit",
            pos_count_ok,
            len(open_pos_list),
            max_pos_cap,
            f"{len(open_pos_list)}/{max_pos_cap} open positions",
        )

        # Check 11: Concentration & rolling return correlation against held symbols
        corr_ok = True
        max_corr_seen = 0.0
        max_corr_peer = None
        correlated_cluster_exposure = current_sym_exposure
        if raw_intent == "BUY" and returns_by_symbol and sym in returns_by_symbol:
            cand_rets = returns_by_symbol[sym]
            for pos in open_pos_list:
                peer_sym = str(pos.get("symbol", "")).upper()
                if peer_sym == sym or peer_sym not in returns_by_symbol:
                    continue
                rho = pearson_return_correlation(cand_rets, returns_by_symbol[peer_sym])
                if rho is not None:
                    if abs(rho) > abs(max_corr_seen):
                        max_corr_seen = rho
                        max_corr_peer = peer_sym
                    if rho >= cfg.max_position_correlation:
                        peer_val = float(pos.get("qty", 0)) * float(pos.get("last_price") or pos.get("avg_price") or 0.0)
                        correlated_cluster_exposure += peer_val
            if max_corr_seen >= cfg.max_position_correlation:
                corr_ok = False
        add_check(
            "concentration_and_correlation",
            corr_ok,
            {"max_corr": round(max_corr_seen, 4), "peer": max_corr_peer},
            f"corr < {cfg.max_position_correlation:.2f}",
            f"Max correlation with held {max_corr_peer} is {max_corr_seen:.2f}"
            if max_corr_peer
            else "No correlated open positions",
        )

        # 5. Stop-Distance Risk-Budget Position Sizing + Lot/Tick Rules + Fee/Edge Gate
        tick_size = float(instrument_meta.get("tick_size") or (0.01 if mkt == "NASDAQ" else 0.05))
        lot_size = max(1, int(instrument_meta.get("quantity_step") or instrument_meta.get("lot_size") or 1))
        product = str(instrument_meta.get("product") or ("D" if mkt == "NSE" else "CASH"))

        selected_qty = 0
        stop_loss_px: float | None = None
        take_profit_px: float | None = None
        estimated_fees = 0.0

        max_sym_exp_abs = min(
            equity * cfg.max_symbol_exposure_pct,
            float(hard.get("max_symbol_exposure", equity * cfg.max_symbol_exposure_pct)),
        )
        max_tot_exp_abs = min(
            equity * cfg.max_total_exposure_pct,
            float(hard.get("max_total_exposure", equity * cfg.max_total_exposure_pct)),
        )
        max_order_val_abs = min(
            equity * cfg.max_order_value_pct,
            float(hard.get("max_order_value", equity * cfg.max_order_value_pct)),
        )

        if raw_intent == "SELL" and held_qty > 0:
            # Exit held long position (rounded to lot_size)
            selected_qty = (int(held_qty) // lot_size) * lot_size
            if selected_qty <= 0:
                selected_qty = int(held_qty)
            if selected_qty > 0 and ref_price > 0:
                estimated_fees = float(
                    estimate_market_fees(
                        market=mkt,
                        side="SELL",
                        qty=selected_qty,
                        fill_price=ref_price,
                        product=product,
                        brokerage_bps=cfg.brokerage_bps,
                    )["total"]
                )
            add_check("symbol_and_total_exposure", True, round(current_sym_exposure, 2), round(max_sym_exp_abs, 2), "Reducing exposure via SELL exit")
            add_check("risk_budget_position_sizing", selected_qty >= lot_size, selected_qty, f">= {lot_size}", f"Exiting {selected_qty} held units")

        elif raw_intent == "BUY" and ref_price > 0:
            stop_dist_px = max(tick_size * 2.0, ref_price * (avg_stop_pct / 100.0))
            stop_loss_px = _round_to_tick(max(tick_size, ref_price - stop_dist_px), tick_size, market=mkt, side="SELL")
            actual_stop_dist = max(tick_size, ref_price - stop_loss_px)
            take_profit_px = _round_to_tick(
                ref_price + actual_stop_dist * cfg.reward_to_risk_multiple,
                tick_size,
                market=mkt,
                side="SELL",
            )

            # Per-unit round-trip friction (spread + slippage + approx brokerage/fees)
            friction_per_unit = ref_price * ((max(spread_bps, 2.0) + 2.0 * cfg.slippage_bps + 2.0 * cfg.brokerage_bps + (22.0 if mkt == "NSE" else 1.0)) / 10_000.0)
            risk_budget_ccy = equity * cfg.per_trade_risk_pct
            qty_by_risk = math.floor(risk_budget_ccy / max(1e-6, actual_stop_dist + friction_per_unit))

            rem_sym_exp = max(0.0, max_sym_exp_abs - current_sym_exposure)
            rem_tot_exp = max(0.0, max_tot_exp_abs - current_total_exposure)
            rem_cluster_exp = max(0.0, equity * cfg.max_correlated_cluster_pct - correlated_cluster_exposure)
            cap_notional = min(max_order_val_abs, rem_sym_exp, rem_tot_exp, rem_cluster_exp, cash * 0.992)

            qty_by_notional = math.floor(cap_notional / ref_price)
            qty_by_liq = math.floor(last_vol * cfg.max_bar_participation) if last_vol > 0 else 0
            raw_qty = max(0, min(qty_by_risk, qty_by_notional, qty_by_liq))
            lot_qty = (raw_qty // lot_size) * lot_size

            # Verify total cash covers notional + itemized regulatory BUY fees
            while lot_qty >= lot_size:
                fee_est = float(
                    estimate_market_fees(
                        market=mkt,
                        side="BUY",
                        qty=lot_qty,
                        fill_price=ref_price * (1.0 + cfg.slippage_bps / 10_000.0),
                        product=product,
                        brokerage_bps=cfg.brokerage_bps,
                    )["total"]
                )
                if lot_qty * ref_price * (1.0 + cfg.slippage_bps / 10_000.0) + fee_est <= cash:
                    estimated_fees = round(fee_est, 4)
                    break
                lot_qty -= lot_size

            selected_qty = max(0, lot_qty)
            exp_ok = rem_sym_exp >= lot_size * ref_price and rem_tot_exp >= lot_size * ref_price
            add_check(
                "symbol_and_total_exposure",
                exp_ok,
                {"symbol_exposure": round(current_sym_exposure, 2), "total_exposure": round(current_total_exposure, 2)},
                {"max_symbol": round(max_sym_exp_abs, 2), "max_total": round(max_tot_exp_abs, 2)},
                "Within symbol and total portfolio exposure limits" if exp_ok else "Symbol or total portfolio exposure cap reached",
            )
            add_check(
                "risk_budget_position_sizing",
                selected_qty >= lot_size,
                {"selected_qty": selected_qty, "lot_size": lot_size, "risk_budget": round(risk_budget_ccy, 2)},
                f">= {lot_size} units",
                f"Risk-sized {selected_qty} units (stop={stop_loss_px}, target={take_profit_px})"
                if selected_qty >= lot_size
                else f"Risk/liquidity/cash budget supports 0 lots (lot_size={lot_size})",
            )
        else:
            add_check(
                "symbol_and_total_exposure",
                True,
                {"symbol_exposure": round(current_sym_exposure, 2), "total_exposure": round(current_total_exposure, 2)},
                {"max_symbol": round(max_sym_exp_abs, 2), "max_total": round(max_tot_exp_abs, 2)},
                "No order proposed",
            )
            add_check(
                "risk_budget_position_sizing",
                False,
                0,
                f">= {lot_size}",
                "No actionable BUY/SELL intent",
            )

        all_passed = all(c["passed"] for c in risk_checks) and selected_qty >= lot_size and raw_intent in {"BUY", "SELL"}
        final_action = raw_intent if all_passed else "NO_TRADE"

        return {
            "market": mkt,
            "symbol": sym,
            "timeframe": timeframe,
            "bar_timestamp": bar_ts,
            "evaluated_at": ref_now.isoformat(),
            "final_action": final_action,
            "raw_intent": raw_intent,
            "selected_qty": selected_qty if final_action in {"BUY", "SELL"} else 0,
            "reference_price": round(ref_price, 4),
            "stop_loss": stop_loss_px if final_action == "BUY" else None,
            "take_profit": take_profit_px if final_action == "BUY" else None,
            "estimated_fees": estimated_fees if final_action in {"BUY", "SELL"} else 0.0,
            "lot_size": lot_size,
            "tick_size": tick_size,
            "product": product,
            "aggregation": {
                "total_signals_received": len(signals),
                "duplicate_variants_deduped": duplicate_skipped,
                "zero_weight_variants_rejected": zero_weight_skipped,
                "active_weighted_variants": sum(1 for s in per_strategy_summary if s["status"] == "WEIGHTED_ACTIVE"),
                "active_families": sorted(active_families),
                "bullish_weight": round(bull_w, 5),
                "bearish_weight": round(bear_w, 5),
                "neutral_weight": round(neutral_w, 5),
                "conflict_ratio": round(conflict_ratio, 5),
                "pre_kronos_consensus": round(pre_kronos_consensus, 5),
                "post_kronos_consensus": round(post_kronos_consensus, 5),
            },
            "kronos_contribution": kronos_contribution,
            "calibration": cal_est,
            "risk_checks": risk_checks,
            "no_trade_reasons": [] if final_action in {"BUY", "SELL"} else no_trade_reasons,
            "strategy_signals": per_strategy_summary,
        }
