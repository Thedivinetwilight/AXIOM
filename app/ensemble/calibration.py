"""Out-of-Sample Empirical Confidence Calibration & Walk-Forward Strategy Weighting.

Key anti-overfitting guarantees:
1. Raw strategy/ensemble scores are NEVER treated as probabilities. Confidence is
   estimated strictly from chronological out-of-sample walk-forward outcomes via
   monotone Pool-Adjacent-Violators (isotonic) + Beta(2, 2) shrinkage, with 90%
   Wilson uncertainty intervals, Brier score, ECE, and regime sample warnings.
2. Strategy weights are learned exclusively on expanding walk-forward OOS folds
   inside the development window [0 : val_end), keeping untouched_test 100% unseen.
3. Multiple-testing Sharpe haircut penalizes large variant libraries (M variants)
   and rejects strategies failing 2.0x cost stress or minimum trade counts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.data.models import Candle
from app.ensemble.registry import (
    SharedIndicatorContext,
    StrategyVariantSpec,
    evaluate_variant_on_context,
)
from app.markets.live_paper import estimate_market_fees

ANNUALIZATION_BARS: dict[str, float] = {
    "1m": 252.0 * 375.0,
    "5m": 252.0 * 75.0,
    "15m": 252.0 * 25.0,
    "30m": 252.0 * 13.0,
    "1h": 252.0 * 6.5,
    "4h": 252.0 * 2.0,
    "1D": 252.0,
}


def classify_bar_regime(ctx: SharedIndicatorContext) -> str:
    """Causally classify trailing completed-bar state into TREND, VOLATILE, or RANGE."""
    rets = ctx.returns()[-20:]
    if len(rets) < 5:
        return "RANGE"
    abs_sum = sum(abs(r) for r in rets)
    net_move = abs(sum(rets))
    efficiency = (net_move / abs_sum) if abs_sum > 1e-12 else 0.0
    vol = math.sqrt(sum(r * r for r in rets) / len(rets))
    if vol >= 0.018:
        return "VOLATILE"
    if efficiency >= 0.36:
        return "TREND"
    return "RANGE"


def wilson_ci90(successes: float, n: int) -> tuple[float, float]:
    """90% Wilson score confidence interval (z = 1.64485) for binomial proportion."""
    if n <= 0:
        return (0.0, 1.0)
    z = 1.6448536
    p = max(0.0, min(1.0, successes / n))
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt((p * (1.0 - p) / n) + ((z * z) / (4.0 * n * n)))
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def _pav_monotone_non_decreasing(values: list[float], weights: list[float]) -> list[float]:
    """Pool-Adjacent-Violators algorithm for weighted monotone isotonic calibration."""
    if not values:
        return []
    blocks: list[list[float]] = [[values[i], weights[i], 1] for i in range(len(values))]
    idx = 0
    while idx < len(blocks) - 1:
        if blocks[idx][0] > blocks[idx + 1][0] + 1e-12:
            w1, w2 = blocks[idx][1], blocks[idx + 1][1]
            tot_w = max(1e-9, w1 + w2)
            merged_val = (blocks[idx][0] * w1 + blocks[idx + 1][0] * w2) / tot_w
            merged_cnt = blocks[idx][2] + blocks[idx + 1][2]
            blocks[idx : idx + 2] = [[merged_val, tot_w, merged_cnt]]
            if idx > 0:
                idx -= 1
        else:
            idx += 1
    out: list[float] = []
    for val, _, cnt in blocks:
        out.extend([val] * int(cnt))
    return out


@dataclass
class CalibrationBin:
    low_edge: float
    high_edge: float
    sample_count: int
    empirical_hit_rate: float
    calibrated_prob: float
    ci90_low: float
    ci90_high: float


class EmpiricalCalibrator:
    """Maps non-probabilistic ensemble consensus magnitude |score| in [0, 1] into
    historically calibrated out-of-sample directional hit probability."""

    def __init__(self, *, num_bins: int = 5, min_regime_samples: int = 12) -> None:
        self.num_bins = max(3, min(int(num_bins), 10))
        self.min_regime_samples = max(5, int(min_regime_samples))
        self.total_oos_samples: int = 0
        self.brier_score: float | None = None
        self.ece: float | None = None
        self.bins: list[CalibrationBin] = []
        self.regime_stats: dict[str, dict[str, Any]] = {}
        self.fitted: bool = False

    def fit_oos(self, observations: list[tuple[float, int, str]]) -> dict[str, Any]:
        """Fit calibration strictly on out-of-sample (abs_score, hit_0_or_1, regime) tuples."""
        clean = [
            (max(0.0, min(1.0, abs(float(sc)))), 1 if int(hit) > 0 else 0, str(reg or "RANGE"))
            for sc, hit, reg in observations
            if math.isfinite(float(sc))
        ]
        self.total_oos_samples = len(clean)
        self.bins = []
        self.regime_stats = {}
        if not clean:
            self.fitted = False
            self.brier_score = None
            self.ece = None
            return self.summary()

        # Partition [0, 1] into fixed width bins + Beta(2, 2) Laplace prior shrinkage
        raw_probs: list[float] = []
        bin_weights: list[float] = []
        bin_counts: list[int] = []
        bin_hits: list[int] = []
        edges = [i / self.num_bins for i in range(self.num_bins + 1)]

        for b_idx in range(self.num_bins):
            lo, hi = edges[b_idx], edges[b_idx + 1]
            in_bin = [
                item for item in clean
                if (lo <= item[0] < hi) or (b_idx == self.num_bins - 1 and lo <= item[0] <= hi)
            ]
            cnt = len(in_bin)
            hits = sum(item[1] for item in in_bin)
            # Shrink small bins toward 0.50 with Beta(2, 2) prior (strength = 4 pseudo-counts)
            shrunk = (hits + 2.0) / (cnt + 4.0)
            raw_probs.append(shrunk)
            bin_weights.append(float(cnt + 4.0))
            bin_counts.append(cnt)
            bin_hits.append(hits)

        iso_probs = _pav_monotone_non_decreasing(raw_probs, bin_weights)
        for b_idx in range(self.num_bins):
            cnt = bin_counts[b_idx]
            hits = bin_hits[b_idx]
            emp = (hits / cnt) if cnt > 0 else 0.5
            ci_lo, ci_hi = wilson_ci90(hits, cnt)
            self.bins.append(
                CalibrationBin(
                    low_edge=round(edges[b_idx], 4),
                    high_edge=round(edges[b_idx + 1], 4),
                    sample_count=cnt,
                    empirical_hit_rate=round(emp, 4),
                    calibrated_prob=round(iso_probs[b_idx], 4),
                    ci90_low=ci_lo,
                    ci90_high=ci_hi,
                )
            )

        # Compute out-of-sample Brier score and Expected Calibration Error (ECE)
        brier_acc = 0.0
        ece_acc = 0.0
        for sc, hit, reg in clean:
            pred = self._lookup_prob(sc)[0]
            brier_acc += (pred - hit) ** 2
            bucket = self.regime_stats.setdefault(reg, {"samples": 0, "hits": 0})
            bucket["samples"] += 1
            bucket["hits"] += hit

        for b in self.bins:
            if b.sample_count > 0:
                ece_acc += (b.sample_count / self.total_oos_samples) * abs(b.calibrated_prob - b.empirical_hit_rate)

        for reg, st in self.regime_stats.items():
            cnt = int(st["samples"])
            hits = int(st["hits"])
            ci_lo, ci_hi = wilson_ci90(hits, cnt)
            st["empirical_hit_rate"] = round(hits / max(1, cnt), 4)
            st["ci90"] = [ci_lo, ci_hi]
            st["sufficient_evidence"] = cnt >= self.min_regime_samples

        self.brier_score = round(brier_acc / self.total_oos_samples, 5)
        self.ece = round(ece_acc, 5)
        self.fitted = True
        return self.summary()

    def _lookup_prob(self, abs_score: float) -> tuple[float, CalibrationBin | None]:
        if not self.bins:
            return (0.5, None)
        x = max(0.0, min(1.0, abs(abs_score)))
        for idx, b in enumerate(self.bins):
            if b.low_edge <= x < b.high_edge or (idx == len(self.bins) - 1 and b.low_edge <= x <= b.high_edge):
                return (b.calibrated_prob, b)
        return (self.bins[-1].calibrated_prob, self.bins[-1])

    def estimate(
        self,
        raw_consensus_score: float,
        *,
        regime: str,
        min_oos_samples: int = 20,
    ) -> dict[str, Any]:
        """Estimate calibrated directional confidence, uncertainty CI, and regime limitations."""
        if not self.fitted or self.total_oos_samples == 0:
            return {
                "calibrated_confidence": None,
                "raw_consensus_score": round(raw_consensus_score, 5),
                "raw_score_is_probability": False,
                "calibration_quality": "UNCALIBRATED",
                "oos_sample_size": 0,
                "bin_sample_size": 0,
                "ci90_low": 0.0,
                "ci90_high": 1.0,
                "brier_score": None,
                "ece": None,
                "regime": regime,
                "regime_sample_size": 0,
                "regime_limitations": [
                    "No out-of-sample walk-forward calibration has been fitted yet; raw scores are never treated as probabilities."
                ],
            }

        prob, matched_bin = self._lookup_prob(abs(raw_consensus_score))
        reg_info = self.regime_stats.get(regime) or {"samples": 0, "empirical_hit_rate": None, "ci90": [0.0, 1.0]}
        reg_n = int(reg_info.get("samples", 0))
        bin_n = matched_bin.sample_count if matched_bin else 0
        limitations: list[str] = []

        if self.total_oos_samples < min_oos_samples:
            limitations.append(
                f"Total OOS calibration sample size ({self.total_oos_samples}) < required minimum ({min_oos_samples})."
            )
        if bin_n < 5:
            limitations.append(
                f"Score bin [{matched_bin.low_edge:.2f}, {matched_bin.high_edge:.2f}] has only {bin_n} OOS observations (shrunk toward 0.50)."
                if matched_bin
                else "Score bin has sparse OOS observations."
            )
        if reg_n < self.min_regime_samples:
            limitations.append(
                f"Regime {regime!r} has only {reg_n} OOS observations (< {self.min_regime_samples}); regime hit-rate is uncertain."
            )
        if self.ece is not None and self.ece > 0.15:
            limitations.append(f"Expected Calibration Error ({self.ece:.3f}) exceeds 0.15 threshold.")

        quality = "CALIBRATED"
        if self.total_oos_samples < min_oos_samples or (self.ece is not None and self.ece > 0.20):
            quality = "INSUFFICIENT_EVIDENCE"
        elif limitations:
            quality = "LOW_SAMPLE_UNCERTAIN"

        return {
            "calibrated_confidence": round(prob, 4),
            "raw_consensus_score": round(raw_consensus_score, 5),
            "raw_score_is_probability": False,
            "calibration_quality": quality,
            "oos_sample_size": self.total_oos_samples,
            "bin_sample_size": bin_n,
            "ci90_low": matched_bin.ci90_low if matched_bin else 0.0,
            "ci90_high": matched_bin.ci90_high if matched_bin else 1.0,
            "brier_score": self.brier_score,
            "ece": self.ece,
            "regime": regime,
            "regime_sample_size": reg_n,
            "regime_empirical_hit_rate": reg_info.get("empirical_hit_rate"),
            "regime_limitations": limitations,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "fitted": self.fitted,
            "total_oos_samples": self.total_oos_samples,
            "brier_score": self.brier_score,
            "ece": self.ece,
            "min_regime_samples": self.min_regime_samples,
            "bins": [
                {
                    "low_edge": b.low_edge,
                    "high_edge": b.high_edge,
                    "sample_count": b.sample_count,
                    "empirical_hit_rate": b.empirical_hit_rate,
                    "calibrated_prob": b.calibrated_prob,
                    "ci90": [b.ci90_low, b.ci90_high],
                }
                for b in self.bins
            ],
            "regime_stats": self.regime_stats,
        }

    def to_state_dict(self) -> dict[str, Any]:
        return self.summary()

    @classmethod
    def from_state_dict(cls, data: dict[str, Any] | None) -> EmpiricalCalibrator:
        inst = cls(min_regime_samples=int((data or {}).get("min_regime_samples", 12)))
        if not data or not data.get("fitted"):
            return inst
        inst.fitted = True
        inst.total_oos_samples = int(data.get("total_oos_samples", 0))
        inst.brier_score = data.get("brier_score")
        inst.ece = data.get("ece")
        inst.regime_stats = dict(data.get("regime_stats") or {})
        inst.bins = [
            CalibrationBin(
                low_edge=float(b["low_edge"]),
                high_edge=float(b["high_edge"]),
                sample_count=int(b["sample_count"]),
                empirical_hit_rate=float(b["empirical_hit_rate"]),
                calibrated_prob=float(b["calibrated_prob"]),
                ci90_low=float(b["ci90"][0]),
                ci90_high=float(b["ci90"][1]),
            )
            for b in (data.get("bins") or [])
        ]
        return inst


def multiple_testing_sharpe_haircut(num_variants_tested: int, oos_bars: int) -> float:
    """Deflated/Bonferroni-inspired minimum OOS Sharpe threshold increasing with ln(M)."""
    m = max(1, int(num_variants_tested))
    n = max(25, int(oos_bars))
    if m <= 1:
        return 0.05
    return round(min(2.25, math.sqrt((2.0 * math.log(float(m))) / float(n)) * 2.2), 4)


def _simulate_variant_oos_fold(
    spec: StrategyVariantSpec,
    bars: list[Candle],
    *,
    start_idx: int,
    end_idx: int,
    market: str,
    timeframe: str,
    cost_mult: float = 1.0,
    spread_bps: float = 4.0,
    slippage_bps: float = 5.0,
    brokerage_bps: float = 3.0,
) -> dict[str, Any]:
    """Causal next-bar-open backtest on bars[start_idx : end_idx] using history up to t."""
    ann = ANNUALIZATION_BARS.get(timeframe, 252.0)
    starting_cash = 1_000_000.0 if market == "NSE" else 100_000.0
    cash = starting_cash
    qty = 0.0
    trades = 0
    bar_rets: list[float] = []
    prev_eq = starting_cash
    friction_bps = (spread_bps * 0.5 + slippage_bps) * cost_mult
    product = "D" if market == "NSE" else "CASH"

    for t in range(max(35, start_idx), end_idx - 1):
        ctx = SharedIndicatorContext(bars[: t + 1], max_tail=160)
        sig = evaluate_variant_on_context(
            spec,
            ctx,
            market=market,
            symbol=bars[t].symbol or "ASSET",
            timeframe=timeframe,
            evaluated_at=bars[t].timestamp.isoformat(),
        )
        nxt = bars[t + 1]
        if sig.direction > 0 and qty <= 0.0:
            exec_px = float(nxt.open) * (1.0 + friction_bps / 10_000.0)
            budget = cash * 0.25
            buy_qty = math.floor(budget / max(1e-6, exec_px))
            if buy_qty >= 1:
                fees = float(
                    estimate_market_fees(
                        market=market,
                        side="BUY",
                        qty=buy_qty,
                        fill_price=exec_px,
                        product=product,
                        brokerage_bps=brokerage_bps * cost_mult,
                    )["total"]
                )
                total_cost = buy_qty * exec_px + fees
                if total_cost <= cash:
                    cash -= total_cost
                    qty += buy_qty
                    trades += 1
        elif sig.direction < 0 and qty > 0.0:
            exec_px = float(nxt.open) * max(0.0001, 1.0 - friction_bps / 10_000.0)
            fees = float(
                estimate_market_fees(
                    market=market,
                    side="SELL",
                    qty=qty,
                    fill_price=exec_px,
                    product=product,
                    brokerage_bps=brokerage_bps * cost_mult,
                )["total"]
            )
            cash += qty * exec_px - fees
            qty = 0.0
            trades += 1

        eq = cash + qty * float(nxt.close)
        bar_rets.append((eq / prev_eq) - 1.0 if prev_eq > 0 else 0.0)
        prev_eq = eq

    if qty > 0.0 and end_idx - 1 < len(bars):
        last_px = float(bars[end_idx - 1].close) * max(0.0001, 1.0 - friction_bps / 10_000.0)
        fees = float(
            estimate_market_fees(
                market=market,
                side="SELL",
                qty=qty,
                fill_price=last_px,
                product=product,
                brokerage_bps=brokerage_bps * cost_mult,
            )["total"]
        )
        cash += qty * last_px - fees
        qty = 0.0

    net_ret_pct = ((cash / starting_cash) - 1.0) * 100.0
    if len(bar_rets) >= 2:
        mean_r = sum(bar_rets) / len(bar_rets)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in bar_rets) / max(1, len(bar_rets) - 1))
        sharpe = (mean_r / std_r) * math.sqrt(ann) if std_r > 1e-12 else 0.0
    else:
        sharpe = 0.0

    return {
        "net_return_pct": round(net_ret_pct, 4),
        "sharpe": round(sharpe, 4),
        "trades": trades,
    }


class WalkForwardWeightLearner:
    """Learns strategy weights & fits EmpiricalCalibrator strictly on expanding
    walk-forward out-of-sample folds prior to the untouched final test split."""

    def fit_weights_and_calibrator(
        self,
        *,
        bars: list[Candle],
        variants: list[StrategyVariantSpec],
        market: str,
        timeframe: str,
        dev_end_idx: int,
        embargo_bars: int = 5,
        folds: int = 3,
        min_oos_trades: int = 2,
        max_single_variant_weight: float = 0.30,
    ) -> dict[str, Any]:
        """Run expanding walk-forward inside bars[:dev_end_idx] ONLY (never touching untouched_test)."""
        if dev_end_idx > len(bars):
            raise ValueError("dev_end_idx exceeds bar count")
        unique_variants = [v for v in variants if v.enabled and v.duplicate_of is None]
        if dev_end_idx < 100 or not unique_variants:
            cal = EmpiricalCalibrator()
            return {
                "weights": {v.variant_id: 0.0 for v in variants},
                "variant_diagnostics": [],
                "calibrator": cal,
                "multiple_testing": {
                    "variants_tested": len(unique_variants),
                    "sharpe_haircut_threshold": 0.0,
                    "accepted_count": 0,
                    "rejected_count": len(unique_variants),
                },
            }

        # Build expanding walk-forward OOS windows inside [0 : dev_end_idx)
        initial_train = max(45, int(dev_end_idx * 0.45))
        remaining = dev_end_idx - initial_train - embargo_bars
        fold_len = max(20, remaining // max(1, folds))
        oos_windows: list[tuple[int, int]] = []
        cursor = initial_train + embargo_bars
        while cursor + 15 <= dev_end_idx and len(oos_windows) < folds:
            w_end = min(dev_end_idx, cursor + fold_len)
            oos_windows.append((cursor, w_end))
            cursor = w_end + embargo_bars

        total_oos_bars = sum(b - a for a, b in oos_windows)
        sharpe_threshold = multiple_testing_sharpe_haircut(len(unique_variants), total_oos_bars)

        raw_weights: dict[str, float] = {}
        diagnostics: list[dict[str, Any]] = []

        for spec in unique_variants:
            fold_sharpes: list[float] = []
            fold_rets_1x: list[float] = []
            fold_rets_2x: list[float] = []
            total_trades = 0
            for w_start, w_end in oos_windows:
                r1 = _simulate_variant_oos_fold(
                    spec, bars, start_idx=w_start, end_idx=w_end, market=market, timeframe=timeframe, cost_mult=1.0
                )
                r2 = _simulate_variant_oos_fold(
                    spec, bars, start_idx=w_start, end_idx=w_end, market=market, timeframe=timeframe, cost_mult=2.0
                )
                fold_sharpes.append(r1["sharpe"])
                fold_rets_1x.append(r1["net_return_pct"])
                fold_rets_2x.append(r2["net_return_pct"])
                total_trades += int(r1["trades"])

            avg_sharpe = sum(fold_sharpes) / max(1, len(fold_sharpes))
            sum_ret_1x = sum(fold_rets_1x)
            sum_ret_2x = sum(fold_rets_2x)
            reasons: list[str] = []
            if total_trades < min_oos_trades:
                reasons.append(f"OOS walk-forward trades ({total_trades}) < {min_oos_trades}")
            if avg_sharpe <= sharpe_threshold:
                reasons.append(f"OOS Sharpe ({avg_sharpe:.2f}) <= multiple-testing haircut ({sharpe_threshold:.2f})")
            if sum_ret_2x <= 0.0:
                reasons.append(f"2.0x stressed OOS net return ({sum_ret_2x:.2f}%) <= 0%")

            surplus = max(0.0, avg_sharpe - sharpe_threshold) if not reasons else 0.0
            raw_weights[spec.variant_id] = surplus
            diagnostics.append(
                {
                    "variant_id": spec.variant_id,
                    "family": spec.family,
                    "canonical_signature": spec.canonical_signature,
                    "oos_trades": total_trades,
                    "oos_avg_sharpe": round(avg_sharpe, 4),
                    "oos_net_return_1x_pct": round(sum_ret_1x, 4),
                    "oos_net_return_2x_pct": round(sum_ret_2x, 4),
                    "sharpe_haircut_applied": sharpe_threshold,
                    "status": "ACCEPTED_OOS" if surplus > 0 else "REJECTED_OOS",
                    "rejection_reasons": reasons,
                }
            )

        tot_surplus = sum(raw_weights.values())
        final_weights: dict[str, float] = {}
        if tot_surplus > 1e-12:
            # Normalize and cap per-variant concentration
            uncapped = {vid: val / tot_surplus for vid, val in raw_weights.items()}
            capped = {vid: min(max_single_variant_weight, w) for vid, w in uncapped.items()}
            cap_sum = sum(capped.values())
            # Only renormalize up to 1.0 if enough distinct variants exist to respect the cap
            if cap_sum > 1e-12 and len([w for w in capped.values() if w > 0]) * max_single_variant_weight >= 1.0:
                final_weights = {vid: round(w / cap_sum, 6) for vid, w in capped.items()}
            else:
                final_weights = {vid: round(w, 6) for vid, w in capped.items()}
        else:
            final_weights = {spec.variant_id: 0.0 for spec in unique_variants}

        # Explicitly assign 0.0 to disabled or duplicate variants
        for v in variants:
            if v.variant_id not in final_weights:
                final_weights[v.variant_id] = 0.0
        for d in diagnostics:
            d["learned_weight"] = final_weights.get(d["variant_id"], 0.0)

        # Fit EmpiricalCalibrator on out-of-sample walk-forward ensemble consensus outcomes
        oos_obs: list[tuple[float, int, str]] = []
        active_specs = [v for v in unique_variants if final_weights.get(v.variant_id, 0.0) > 0]
        eval_specs = active_specs if active_specs else unique_variants[: min(12, len(unique_variants))]
        use_equal_fallback_for_cal = not active_specs

        for w_start, w_end in oos_windows:
            for t in range(max(35, w_start), w_end - 2):
                ctx = SharedIndicatorContext(bars[: t + 1], max_tail=160)
                regime = classify_bar_regime(ctx)
                w_sum = 0.0
                weighted_score = 0.0
                for sp in eval_specs:
                    w = (1.0 / len(eval_specs)) if use_equal_fallback_for_cal else final_weights.get(sp.variant_id, 0.0)
                    if w <= 0:
                        continue
                    sig = evaluate_variant_on_context(
                        sp,
                        ctx,
                        market=market,
                        symbol=bars[t].symbol or "ASSET",
                        timeframe=timeframe,
                        evaluated_at=bars[t].timestamp.isoformat(),
                    )
                    weighted_score += w * (float(sig.direction) * 0.65 + float(sig.raw_score) * 0.35)
                    w_sum += w
                consensus = (weighted_score / w_sum) if w_sum > 0 else 0.0
                if abs(consensus) < 0.05:
                    continue
                fwd_ret = (float(bars[t + 2].open) / float(bars[t + 1].open)) - 1.0
                hit = 1 if (consensus > 0 and fwd_ret > 0) or (consensus < 0 and fwd_ret < 0) else 0
                oos_obs.append((abs(consensus), hit, regime))

        calibrator = EmpiricalCalibrator()
        calibrator.fit_oos(oos_obs)
        accepted_cnt = sum(1 for d in diagnostics if d["status"] == "ACCEPTED_OOS")
        return {
            "weights": final_weights,
            "variant_diagnostics": diagnostics,
            "calibrator": calibrator,
            "oos_windows": [
                {
                    "start_idx": a,
                    "end_idx": b,
                    "start_ts": bars[a].timestamp.isoformat(),
                    "end_ts": bars[b - 1].timestamp.isoformat(),
                    "bars": b - a,
                }
                for a, b in oos_windows
            ],
            "multiple_testing": {
                "variants_tested": len(unique_variants),
                "total_oos_bars": total_oos_bars,
                "sharpe_haircut_threshold": sharpe_threshold,
                "accepted_count": accepted_cnt,
                "rejected_count": len(unique_variants) - accepted_cnt,
            },
        }
