"""Leak-Free Multi-Market Research, Walk-Forward & Cost-Stress Pipeline (PAPER/RESEARCH ONLY).

Implements:
1. Strict chronological ``train -> embargo -> validation -> embargo -> untouched_test`` splits
   plus expanding walk-forward folds across ``TREND``, ``RANGE``, and ``VOLATILE`` regimes.
2. Strictly causal lagged bar features (features at ``t`` read ``bars[:t+1]`` only;
   supervised targets on ``train`` satisfy ``t + 2 < train_end`` so targets never cross
   into ``embargo`` or ``validation``; fills execute at ``bars[t + 1].open``).
3. Mandatory baseline comparison (``buy_and_hold``, ``no_trade``, tuned rule strategies,
   Kronos-free ``axiom_linear_lagged`` Ridge decision model, and optional ``kronos_ensemble``).
4. Market-accurate NSE (INR) and NASDAQ (USD) fees, spread, slippage, partial-fill
   participation limits, and ``2.0x`` cost-stress rejection gates.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.data.models import Candle
from app.markets.live_paper import estimate_market_fees
from app.model_lab.kronos_provider import KronosForecastProvider, KronosUnavailableError

FEATURE_NAMES: tuple[str, ...] = (
    "ret_1",
    "ret_3",
    "ret_5",
    "ret_10",
    "realized_vol_10",
    "hl_range_pct",
    "sma5_vs_sma20",
    "rsi14_centered",
    "volume_rel_10",
)
MIN_LOOKBACK_BARS = 22


class LeakageViolationError(ValueError):
    """Raised if timestamps, splits, or feature windows violate strict causality."""


class ResearchCancelledError(RuntimeError):
    """Raised when an operator cancels an in-progress research run."""


@dataclass(frozen=True)
class ChronologicalSplit:
    total_bars: int
    train_start: int
    train_end: int
    val_start: int
    val_end: int
    test_start: int
    test_end: int
    embargo_bars: int


def split_chronological(
    bars: list[Candle],
    *,
    train_frac: float = 0.60,
    val_frac: float = 0.20,
    embargo_bars: int = 5,
) -> ChronologicalSplit:
    n = len(bars)
    if n < 120:
        raise ValueError(f"At least 120 chronological bars are required for train/val/test research (got {n})")
    if not (0.40 <= train_frac <= 0.75) or not (0.10 <= val_frac <= 0.30):
        raise ValueError("Invalid split fractions; require 0.40<=train_frac<=0.75 and 0.10<=val_frac<=0.30")
    if train_frac + val_frac >= 0.90:
        raise ValueError("Untouched test holdout must retain at least 10% of bars")
    if embargo_bars < 1:
        raise ValueError("embargo_bars must be >= 1 to prevent boundary target leakage")

    for i in range(1, n):
        if bars[i].timestamp <= bars[i - 1].timestamp:
            raise LeakageViolationError(f"Non-monotonic bar timestamp at index {i}")

    train_end = int(n * train_frac)
    val_start = train_end + embargo_bars
    val_end = val_start + int(n * val_frac)
    test_start = val_end + embargo_bars
    test_end = n

    if val_start >= val_end or test_start >= test_end or (test_end - test_start) < 25:
        raise ValueError("Insufficient bars after applying embargo gaps between train, validation, and test")

    if not (
        bars[train_end - 1].timestamp
        < bars[val_start].timestamp
        < bars[val_end - 1].timestamp
        < bars[test_start].timestamp
    ):
        raise LeakageViolationError("Chronological boundary ordering violated across train/validation/test")

    return ChronologicalSplit(
        total_bars=n,
        train_start=0,
        train_end=train_end,
        val_start=val_start,
        val_end=val_end,
        test_start=test_start,
        test_end=test_end,
        embargo_bars=embargo_bars,
    )


def build_walk_forward_folds(
    total_dev_bars: int,
    *,
    n_folds: int = 3,
    embargo_bars: int = 3,
) -> list[tuple[int, int, int, int]]:
    """Return expanding ``(fold_train_start, fold_train_end, fold_val_start, fold_val_end)`` within ``[0, val_end)``."""
    folds: list[tuple[int, int, int, int]] = []
    if total_dev_bars < 90 or n_folds < 2:
        return folds
    chunk = total_dev_bars // (n_folds + 1)
    for k in range(1, n_folds + 1):
        tr_end = chunk * k
        v_start = tr_end + embargo_bars
        v_end = min(total_dev_bars, tr_end + chunk)
        if tr_end >= MIN_LOOKBACK_BARS + 15 and v_end - v_start >= 10:
            folds.append((0, tr_end, v_start, v_end))
    return folds


def classify_regime_causal(prefix_bars: list[Candle]) -> str:
    """Classify bar regime strictly from ``prefix_bars`` (``TREND``, ``VOLATILE``, ``RANGE``)."""
    if len(prefix_bars) < 20:
        return "RANGE"
    closes = [b.close for b in prefix_bars[-21:]]
    rets = [(closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes))]
    mean_r = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets))
    net_move = abs((closes[-1] / closes[0]) - 1.0)
    if vol > 0.018:
        return "VOLATILE"
    if net_move > max(0.025, vol * math.sqrt(20.0) * 1.15):
        return "TREND"
    return "RANGE"


def _solve_linear_system(a_mat: list[list[float]], b_vec: list[float]) -> list[float]:
    """Deterministic Gaussian elimination with partial pivoting (pure stdlib)."""
    n = len(b_vec)
    aug = [list(a_mat[i]) + [float(b_vec[i])] for i in range(n)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-15:
            aug[pivot_row][col] = 1e-12
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        piv = aug[col][col]
        for r in range(col + 1, n):
            factor = aug[r][col] / piv
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    sol = [0.0] * n
    for i in range(n - 1, -1, -1):
        rhs = aug[i][n] - sum(aug[i][j] * sol[j] for j in range(i + 1, n))
        sol[i] = rhs / aug[i][i]
    return sol


def _quantile_sorted(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    w = pos - lo
    return sorted_vals[lo] * (1.0 - w) + sorted_vals[hi] * w


def compute_causal_feature_vector(prefix_bars: list[Candle]) -> list[float]:
    """Compute 9 lagged features strictly from ``prefix_bars`` (no future bars exist in scope)."""
    if len(prefix_bars) < MIN_LOOKBACK_BARS:
        raise LeakageViolationError(f"Need >= {MIN_LOOKBACK_BARS} causal bars; got {len(prefix_bars)}")
    closes = [b.close for b in prefix_bars[-22:]]
    vols = [float(b.volume) for b in prefix_bars[-12:]]
    curr = prefix_bars[-1]

    ret_1 = (closes[-1] / closes[-2]) - 1.0
    ret_3 = (closes[-1] / closes[-4]) - 1.0
    ret_5 = (closes[-1] / closes[-6]) - 1.0
    ret_10 = (closes[-1] / closes[-11]) - 1.0
    rets_10 = [(closes[-11 + i + 1] / closes[-11 + i]) - 1.0 for i in range(10)]
    mean_10 = sum(rets_10) / 10.0
    vol_10 = math.sqrt(sum((r - mean_10) ** 2 for r in rets_10) / 10.0)
    hl_range = (curr.high - curr.low) / curr.close if curr.close > 0 else 0.0
    sma5 = sum(closes[-5:]) / 5.0
    sma20 = sum(closes[-20:]) / 20.0
    sma_diff = (sma5 / sma20) - 1.0 if sma20 > 0 else 0.0

    gains, losses = 0.0, 0.0
    for i in range(-14, 0):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0.0)
        losses += max(-diff, 0.0)
    rsi = 100.0 if losses == 0 else 100.0 - (100.0 / (1.0 + (gains / 14.0) / (losses / 14.0)))
    rsi_centered = (rsi - 50.0) / 50.0

    avg_vol_10 = sum(vols[:-1]) / max(1, len(vols) - 1)
    vol_rel = (vols[-1] / avg_vol_10) - 1.0 if avg_vol_10 > 0 else 0.0
    vol_rel = max(-1.0, min(5.0, vol_rel))

    return [ret_1, ret_3, ret_5, ret_10, vol_10, hl_range, sma_diff, rsi_centered, vol_rel]


@dataclass
class AxiomLaggedRidgeModel:
    """Interpretable, deterministic Ridge-regularized directional model fitted on train only."""

    feature_names: tuple[str, ...]
    mean: list[float]
    std: list[float]
    weights: list[float]
    intercept: float
    alpha: float
    entry_threshold: float

    def predict_expected_return(self, feat: list[float]) -> float:
        dot = sum(((feat[j] - self.mean[j]) / self.std[j]) * self.weights[j] for j in range(len(self.weights)))
        return float(dot + self.intercept)

    def to_artifact_dict(self) -> dict[str, Any]:
        return {
            "model_kind": "AxiomLaggedRidgeModel",
            "feature_names": list(self.feature_names),
            "mean": [round(float(x), 10) for x in self.mean],
            "std": [round(float(x), 10) for x in self.std],
            "weights": [round(float(x), 10) for x in self.weights],
            "intercept": round(float(self.intercept), 10),
            "alpha": float(self.alpha),
            "entry_threshold": float(self.entry_threshold),
        }


def fit_axiom_ridge_on_train(
    train_bars: list[Candle],
    *,
    alpha: float = 1.0,
    entry_threshold: float = 0.001,
) -> AxiomLaggedRidgeModel:
    """Fit closed-form Ridge model strictly inside ``train_bars`` (``idx + 2 < len(train_bars)``)."""
    n_train = len(train_bars)
    if n_train < MIN_LOOKBACK_BARS + 12:
        raise ValueError("Insufficient training bars to fit AxiomLaggedRidgeModel")
    xs: list[list[float]] = []
    ys: list[float] = []
    # Supervised target at idx uses open[idx+1] -> open[idx+2], both strictly inside train_bars!
    for idx in range(MIN_LOOKBACK_BARS - 1, n_train - 2):
        prefix = train_bars[: idx + 1]
        feat = compute_causal_feature_vector(prefix)
        entry_open = train_bars[idx + 1].open
        exit_open = train_bars[idx + 2].open
        fwd_ret = (exit_open - entry_open) / entry_open if entry_open > 0 else 0.0
        xs.append(feat)
        ys.append(fwd_ret)

    n_obs = len(xs)
    dim = len(FEATURE_NAMES)
    mu = [sum(row[j] for row in xs) / n_obs for j in range(dim)]
    sigma = []
    for j in range(dim):
        var_j = sum((row[j] - mu[j]) ** 2 for row in xs) / n_obs
        sd_j = math.sqrt(var_j)
        sigma.append(sd_j if sd_j >= 1e-9 else 1.0)

    x_std = [[(row[j] - mu[j]) / sigma[j] for j in range(dim)] for row in xs]
    y_mean = sum(ys) / n_obs
    y_centered = [y - y_mean for y in ys]

    gram = [[0.0] * dim for _ in range(dim)]
    rhs = [0.0] * dim
    for i in range(n_obs):
        xi = x_std[i]
        yi = y_centered[i]
        for r in range(dim):
            rhs[r] += xi[r] * yi
            for c in range(dim):
                gram[r][c] += xi[r] * xi[c]
    for d in range(dim):
        gram[d][d] += float(alpha)

    weights = _solve_linear_system(gram, rhs)

    return AxiomLaggedRidgeModel(
        feature_names=FEATURE_NAMES,
        mean=mu,
        std=sigma,
        weights=weights,
        intercept=y_mean,
        alpha=float(alpha),
        entry_threshold=float(entry_threshold),
    )


def _market_fee(market: str, notional: float, qty: float, side: str) -> float:
    if notional <= 0 or qty <= 0:
        return 0.0
    px = notional / qty
    return float(
        estimate_market_fees(
            market=market,
            side=side,
            qty=qty,
            fill_price=px,
            product="D" if market == "NSE" else "CASH",
            brokerage_bps=2.5 if market == "NSE" else 0.0,
        )["total"]
    )


def _annualization_factor(timeframe: str, market: str) -> float:
    bars_per_day = {
        "1m": 375.0 if market == "NSE" else 390.0,
        "5m": 75.0 if market == "NSE" else 78.0,
        "15m": 25.0 if market == "NSE" else 26.0,
        "30m": 12.5 if market == "NSE" else 13.0,
        "1h": 6.25 if market == "NSE" else 6.5,
        "1d": 1.0,
    }.get(timeframe, 1.0)
    return math.sqrt(252.0 * bars_per_day)


def _block_bootstrap_ci90(bar_returns: list[float], ann_factor: float) -> dict[str, float]:
    n = len(bar_returns)
    if n < 8:
        return {
            "net_return_ci90_low_pct": 0.0,
            "net_return_ci90_high_pct": 0.0,
            "sharpe_ci90_low": 0.0,
            "sharpe_ci90_high": 0.0,
        }
    rng = random.Random(20261030)
    block = max(4, min(16, n // 5))
    n_blocks = max(1, math.ceil(n / block))
    max_start = max(1, n - block + 1)
    ret_samples: list[float] = []
    sharpe_samples: list[float] = []
    for _ in range(150):
        sample: list[float] = []
        for _b in range(n_blocks):
            s = rng.randrange(0, max_start)
            sample.extend(bar_returns[s : min(n, s + block)])
        sample = sample[:n]
        comp_prod = 1.0
        for r in sample:
            comp_prod *= 1.0 + r
        ret_samples.append((comp_prod - 1.0) * 100.0)
        m = sum(sample) / len(sample)
        sd = math.sqrt(sum((r - m) ** 2 for r in sample) / len(sample))
        sharpe_samples.append(((m / sd) * ann_factor) if sd > 1e-12 else 0.0)
    ret_samples.sort()
    sharpe_samples.sort()
    return {
        "net_return_ci90_low_pct": round(_quantile_sorted(ret_samples, 0.05), 4),
        "net_return_ci90_high_pct": round(_quantile_sorted(ret_samples, 0.95), 4),
        "sharpe_ci90_low": round(_quantile_sorted(sharpe_samples, 0.05), 4),
        "sharpe_ci90_high": round(_quantile_sorted(sharpe_samples, 0.95), 4),
    }


def simulate_causal_segment(
    bars: list[Candle],
    *,
    eval_start: int,
    eval_end: int,
    market: str,
    timeframe: str,
    signal_fn: Callable[[list[Candle]], int],
    initial_capital: float = 100_000.0,
    spread_bps: float = 4.0,
    slippage_bps: float = 5.0,
    cost_multiplier: float = 1.0,
    max_participation_rate: float = 0.05,
    position_fraction: float = 0.25,
) -> dict[str, Any]:
    """Simulate ``[eval_start, eval_end)`` where signal at ``t`` reads ``bars[:t+1]`` and fills at ``bars[t+1].open``."""
    if eval_end <= eval_start + 2:
        raise ValueError("Segment too short for bar simulation")
    cash = float(initial_capital)
    gross_pnl_accum = 0.0
    total_fees_and_costs = 0.0
    total_notional_traded = 0.0
    shares = 0.0
    entry_cost_basis = 0.0

    equity_curve: list[float] = [initial_capital]
    bar_returns: list[float] = []
    regime_returns: dict[str, list[float]] = {"TREND": [], "RANGE": [], "VOLATILE": []}
    closed_trade_pnls: list[float] = []
    bars_in_position = 0
    partial_fills = 0

    eff_half_spread = (spread_bps * 0.5 * cost_multiplier) / 10_000.0
    eff_slip = (slippage_bps * cost_multiplier) / 10_000.0
    friction_rate = eff_half_spread + eff_slip

    for t in range(eval_start, eval_end - 1):
        prefix = bars[: t + 1]
        # Defensive copy slice guarantees signal_fn cannot index future bars!
        desired_pos = int(signal_fn(prefix))  # 1 = long, 0 = flat
        desired_pos = 1 if desired_pos > 0 else 0

        next_bar = bars[t + 1]
        raw_open = float(next_bar.open)
        max_liq_qty = max(1.0, float(next_bar.volume) * max_participation_rate) if next_bar.volume > 0 else 1e9

        if desired_pos == 1 and shares <= 0.0:
            exec_px = raw_open * (1.0 + friction_rate)
            target_budget = cash * position_fraction
            desired_qty = math.floor(target_budget / exec_px) if market == "NSE" else round(target_budget / exec_px, 4)
            fill_qty = min(desired_qty, max_liq_qty)
            if market == "NSE":
                fill_qty = float(math.floor(fill_qty))
            if fill_qty > 0:
                if fill_qty < desired_qty:
                    partial_fills += 1
                notional = fill_qty * exec_px
                fee = _market_fee(market, notional, fill_qty, "BUY")
                if notional + fee <= cash:
                    cash -= notional + fee
                    shares = fill_qty
                    entry_cost_basis = notional + fee
                    total_notional_traded += notional
                    total_fees_and_costs += fee + (fill_qty * raw_open * friction_rate)

        elif desired_pos == 0 and shares > 0.0:
            sell_qty = min(shares, max_liq_qty)
            if market == "NSE":
                sell_qty = float(math.floor(sell_qty)) if sell_qty >= 1.0 else shares
            if sell_qty < shares:
                partial_fills += 1
            exec_px = raw_open * (1.0 - friction_rate)
            notional = sell_qty * exec_px
            fee = _market_fee(market, notional, sell_qty, "SELL")
            fraction_closed = sell_qty / shares
            allocated_basis = entry_cost_basis * fraction_closed
            trade_net_pnl = (notional - fee) - allocated_basis
            closed_trade_pnls.append(trade_net_pnl)
            gross_pnl_accum += (sell_qty * raw_open) - allocated_basis
            cash += notional - fee
            shares -= sell_qty
            entry_cost_basis -= allocated_basis
            if shares < 1e-8:
                shares = 0.0
                entry_cost_basis = 0.0
            total_notional_traded += notional
            total_fees_and_costs += fee + (sell_qty * raw_open * friction_rate)

        if shares > 0.0:
            bars_in_position += 1
        mark_equity = cash + shares * float(next_bar.close)
        prev_eq = equity_curve[-1]
        step_ret = ((mark_equity / prev_eq) - 1.0) if prev_eq > 0 else 0.0
        equity_curve.append(mark_equity)
        bar_returns.append(step_ret)
        regime = classify_regime_causal(prefix)
        regime_returns[regime].append(step_ret)

    # Mark-to-market liquidate open position at end of segment for fair closed-trade accounting
    if shares > 0.0:
        last_close = float(bars[eval_end - 1].close)
        exec_px = last_close * (1.0 - friction_rate)
        notional = shares * exec_px
        fee = _market_fee(market, notional, shares, "SELL")
        trade_net_pnl = (notional - fee) - entry_cost_basis
        closed_trade_pnls.append(trade_net_pnl)
        cash += notional - fee
        total_notional_traded += notional
        total_fees_and_costs += fee + (shares * last_close * friction_rate)
        shares = 0.0
        equity_curve[-1] = cash

    final_equity = equity_curve[-1]
    net_return_pct = ((final_equity / initial_capital) - 1.0) * 100.0
    gross_return_pct = (((final_equity + total_fees_and_costs) / initial_capital) - 1.0) * 100.0

    ann = _annualization_factor(timeframe, market)
    if bar_returns:
        mean_r = sum(bar_returns) / len(bar_returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in bar_returns) / len(bar_returns))
    else:
        mean_r = 0.0
        std_r = 0.0
    sharpe = (mean_r / std_r) * ann if std_r > 1e-12 else 0.0
    downside = [r for r in bar_returns if r < 0.0]
    down_std = math.sqrt(sum(r * r for r in downside) / len(downside)) if downside else 0.0
    sortino = (mean_r / down_std) * ann if down_std > 1e-12 else (sharpe if mean_r <= 0 else 0.0)

    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            dd = ((peak - eq) / peak) * 100.0
            max_dd = max(max_dd, dd)

    wins = [p for p in closed_trade_pnls if p > 0]
    losses = [abs(p) for p in closed_trade_pnls if p < 0]
    trade_count = len(closed_trade_pnls)
    win_rate = (len(wins) / trade_count) * 100.0 if trade_count > 0 else 0.0
    profit_factor = (sum(wins) / sum(losses)) if sum(losses) > 1e-9 else (2.5 if wins else 0.0)
    n_steps = max(1, eval_end - 1 - eval_start)
    exposure_pct = (bars_in_position / n_steps) * 100.0
    turnover = total_notional_traded / initial_capital

    reg_summary: dict[str, dict[str, Any]] = {}
    for reg_name, r_list in regime_returns.items():
        if not r_list:
            reg_summary[reg_name] = {"bars": 0, "return_pct": 0.0}
        else:
            comp_prod = 1.0
            for r in r_list:
                comp_prod *= 1.0 + r
            comp = (comp_prod - 1.0) * 100.0
            reg_summary[reg_name] = {"bars": len(r_list), "return_pct": round(comp, 4)}

    return {
        "net_return_pct": round(net_return_pct, 4),
        "gross_return_pct": round(gross_return_pct, 4),
        "total_fees_and_costs": round(total_fees_and_costs, 2),
        "final_equity": round(final_equity, 2),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "profit_factor": round(profit_factor, 4),
        "win_rate_pct": round(win_rate, 2),
        "turnover_x": round(turnover, 4),
        "exposure_pct": round(exposure_pct, 2),
        "trade_count": trade_count,
        "partial_fills": partial_fills,
        "uncertainty": _block_bootstrap_ci90(bar_returns, ann),
        "regime_breakdown": reg_summary,
    }


def _make_ma_signal(fast: int, slow: int) -> Callable[[list[Candle]], int]:
    def _sig(prefix: list[Candle]) -> int:
        if len(prefix) < slow:
            return 0
        closes = [b.close for b in prefix[-slow:]]
        return 1 if (sum(closes[-fast:]) / fast) > (sum(closes) / slow) else 0

    return _sig


def _make_rsi_signal(oversold: float, overbought: float) -> Callable[[list[Candle]], int]:
    state = {"pos": 0}

    def _sig(prefix: list[Candle]) -> int:
        if len(prefix) < 16:
            return 0
        closes = [b.close for b in prefix[-15:]]
        gains = sum(max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))) / 14.0
        losses = sum(max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))) / 14.0
        rsi = 100.0 if losses == 0 else 100.0 - (100.0 / (1.0 + gains / losses))
        if rsi < oversold:
            state["pos"] = 1
        elif rsi > overbought:
            state["pos"] = 0
        return state["pos"]

    return _sig


def _make_breakout_signal(lookback: int) -> Callable[[list[Candle]], int]:
    state = {"pos": 0}

    def _sig(prefix: list[Candle]) -> int:
        if len(prefix) < lookback + 1:
            return 0
        prior = prefix[-(lookback + 1) : -1]
        hi = max(b.high for b in prior)
        lo = min(b.low for b in prior)
        cl = prefix[-1].close
        if cl > hi:
            state["pos"] = 1
        elif cl < lo:
            state["pos"] = 0
        return state["pos"]

    return _sig


def _make_ridge_signal(model: AxiomLaggedRidgeModel) -> Callable[[list[Candle]], int]:
    def _sig(prefix: list[Candle]) -> int:
        if len(prefix) < MIN_LOOKBACK_BARS:
            return 0
        pred = model.predict_expected_return(compute_causal_feature_vector(prefix))
        return 1 if pred > model.entry_threshold else 0

    return _sig


def execute_research_pipeline(
    *,
    dataset_meta: dict[str, Any],
    bars: list[Candle],
    market: str,
    timeframe: str,
    train_frac: float = 0.60,
    val_frac: float = 0.20,
    embargo_bars: int = 5,
    spread_bps: float = 4.0,
    slippage_bps: float = 5.0,
    stress_multiplier: float = 2.0,
    min_trades: int = 5,
    include_kronos: bool = False,
    kronos_provider: KronosForecastProvider | None = None,
    progress_cb: Callable[[str, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run leak-free baseline + tuned rules + AXIOM Ridge + optional Kronos comparison."""
    mkt = market.strip().upper()
    tf_norm = "1d" if timeframe == "1D" else timeframe.strip()
    initial_capital = 1_000_000.0 if mkt == "NSE" else 100_000.0

    def _step(phase: str, pct: int, detail: str) -> None:
        if cancel_check is not None and cancel_check():
            raise ResearchCancelledError("Research run cancelled by operator")
        if progress_cb is not None:
            progress_cb(phase, pct, detail)

    _step("PARTITIONING", 5, "Validating strict chronological train/embargo/validation/untouched-test split")
    split = split_chronological(
        bars,
        train_frac=train_frac,
        val_frac=val_frac,
        embargo_bars=embargo_bars,
    )
    wf_folds = build_walk_forward_folds(split.val_end, n_folds=3, embargo_bars=max(2, embargo_bars // 2))

    def _eval_all_splits(sig_factory: Callable[[], Callable[[list[Candle]], int]]) -> dict[str, Any]:
        train_res = simulate_causal_segment(
            bars,
            eval_start=max(MIN_LOOKBACK_BARS, split.train_start),
            eval_end=split.train_end,
            market=mkt,
            timeframe=tf_norm,
            signal_fn=sig_factory(),
            initial_capital=initial_capital,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            cost_multiplier=1.0,
        )
        val_res = simulate_causal_segment(
            bars,
            eval_start=split.val_start,
            eval_end=split.val_end,
            market=mkt,
            timeframe=tf_norm,
            signal_fn=sig_factory(),
            initial_capital=initial_capital,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            cost_multiplier=1.0,
        )
        wf_fold_metrics = []
        for f_idx, (_fts, _fte, fvs, fve) in enumerate(wf_folds):
            fm = simulate_causal_segment(
                bars,
                eval_start=fvs,
                eval_end=fve,
                market=mkt,
                timeframe=tf_norm,
                signal_fn=sig_factory(),
                initial_capital=initial_capital,
                spread_bps=spread_bps,
                slippage_bps=slippage_bps,
                cost_multiplier=1.0,
            )
            wf_fold_metrics.append(
                {
                    "fold": f_idx + 1,
                    "val_start_ts": bars[fvs].timestamp.isoformat(),
                    "val_end_ts": bars[fve - 1].timestamp.isoformat(),
                    "net_return_pct": fm["net_return_pct"],
                    "sharpe": fm["sharpe"],
                    "trade_count": fm["trade_count"],
                }
            )
        return {"train": train_res, "validation": val_res, "walk_forward_folds": wf_fold_metrics}

    candidates: list[dict[str, Any]] = []

    # 1. Baseline: Buy and Hold
    _step("BASELINES", 18, "Evaluating buy_and_hold and no_trade baselines")
    bh_dev = _eval_all_splits(lambda: (lambda _p: 1))
    bh_test = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=lambda _p: 1,
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=1.0,
    )
    bh_stress = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=lambda _p: 1,
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=stress_multiplier,
    )
    candidates.append(
        {
            "candidate_id": "buy_and_hold",
            "family": "BASELINE",
            "uses_kronos": False,
            "parameters": {"position": "long_1x"},
            **bh_dev,
            "untouched_test": bh_test,
            "stressed_test": bh_stress,
            "promotion_eligible": False,
            "rejection_reasons": ["Passive benchmark baseline (not a promoted active model)"],
        }
    )

    # 2. Baseline: No Trade (Cash 0%)
    nt_dev = _eval_all_splits(lambda: (lambda _p: 0))
    nt_test = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=lambda _p: 0,
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=1.0,
    )
    candidates.append(
        {
            "candidate_id": "no_trade",
            "family": "BASELINE",
            "uses_kronos": False,
            "parameters": {"position": "flat_cash"},
            **nt_dev,
            "untouched_test": nt_test,
            "stressed_test": nt_test,
            "promotion_eligible": False,
            "rejection_reasons": ["Zero-exposure cash baseline"],
        }
    )

    def _gate_decision(val_m: dict[str, Any], test_m: dict[str, Any], stress_m: dict[str, Any]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if val_m["sharpe"] <= 0:
            reasons.append(f"Validation Sharpe <= 0 ({val_m['sharpe']:.2f})")
        if val_m["trade_count"] < min_trades:
            reasons.append(f"Validation trade_count ({val_m['trade_count']}) < min_trades ({min_trades})")
        if test_m["trade_count"] < min_trades:
            reasons.append(f"Untouched test trade_count ({test_m['trade_count']}) < min_trades ({min_trades})")
        if stress_m["net_return_pct"] <= 0:
            reasons.append(
                f"Failed {stress_multiplier:.1f}x cost-stress gate (stressed net return {stress_m['net_return_pct']:.2f}% <= 0%)"
            )
        return (len(reasons) == 0), reasons

    # 3. Rule Strategy Parameter Tuning (Train + Validation ONLY; Test untouched until selection finishes!)
    _step("RULE_TUNING", 38, "Tuning Moving Average, RSI, and Breakout parameters on train/validation only")
    best_ma_cfg = (5, 20)
    best_ma_dev: dict[str, Any] | None = None
    for fast_w, slow_w in ((5, 20), (8, 26), (10, 30)):
        dev = _eval_all_splits(lambda f=fast_w, s=slow_w: _make_ma_signal(f, s))
        if best_ma_dev is None or dev["validation"]["sharpe"] > best_ma_dev["validation"]["sharpe"]:
            best_ma_cfg = (fast_w, slow_w)
            best_ma_dev = dev
    assert best_ma_dev is not None
    ma_test = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=_make_ma_signal(*best_ma_cfg),
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=1.0,
    )
    ma_stress = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=_make_ma_signal(*best_ma_cfg),
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=stress_multiplier,
    )
    ma_ok, ma_reasons = _gate_decision(best_ma_dev["validation"], ma_test, ma_stress)
    candidates.append(
        {
            "candidate_id": "moving_average_tuned",
            "family": "RULE_STRATEGY",
            "uses_kronos": False,
            "parameters": {"fast_window": best_ma_cfg[0], "slow_window": best_ma_cfg[1]},
            **best_ma_dev,
            "untouched_test": ma_test,
            "stressed_test": ma_stress,
            "promotion_eligible": ma_ok,
            "rejection_reasons": ma_reasons,
        }
    )

    best_rsi_cfg = (30.0, 70.0)
    best_rsi_dev: dict[str, Any] | None = None
    for os_lvl, ob_lvl in ((28.0, 72.0), (30.0, 70.0), (35.0, 65.0)):
        dev = _eval_all_splits(lambda a=os_lvl, b=ob_lvl: _make_rsi_signal(a, b))
        if best_rsi_dev is None or dev["validation"]["sharpe"] > best_rsi_dev["validation"]["sharpe"]:
            best_rsi_cfg = (os_lvl, ob_lvl)
            best_rsi_dev = dev
    assert best_rsi_dev is not None
    rsi_test = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=_make_rsi_signal(*best_rsi_cfg),
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=1.0,
    )
    rsi_stress = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=_make_rsi_signal(*best_rsi_cfg),
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=stress_multiplier,
    )
    rsi_ok, rsi_reasons = _gate_decision(best_rsi_dev["validation"], rsi_test, rsi_stress)
    candidates.append(
        {
            "candidate_id": "rsi_tuned",
            "family": "RULE_STRATEGY",
            "uses_kronos": False,
            "parameters": {"oversold": best_rsi_cfg[0], "overbought": best_rsi_cfg[1]},
            **best_rsi_dev,
            "untouched_test": rsi_test,
            "stressed_test": rsi_stress,
            "promotion_eligible": rsi_ok,
            "rejection_reasons": rsi_reasons,
        }
    )

    # 4. Separate Kronos-Free AXIOM Decision Model (Ridge on causal lagged features)
    _step("AXIOM_MODEL_FIT", 65, "Fitting Kronos-free AxiomLaggedRidgeModel strictly on train bars; selecting hyperparams on validation")
    train_only_bars = bars[split.train_start : split.train_end]
    best_model: AxiomLaggedRidgeModel | None = None
    best_model_dev: dict[str, Any] | None = None
    for alpha in (0.25, 1.0, 5.0):
        for thresh in (0.0, 0.0004, 0.001):
            fitted = fit_axiom_ridge_on_train(train_only_bars, alpha=alpha, entry_threshold=thresh)
            dev = _eval_all_splits(lambda m=fitted: _make_ridge_signal(m))
            if best_model_dev is None or dev["validation"]["sharpe"] > best_model_dev["validation"]["sharpe"]:
                best_model = fitted
                best_model_dev = dev
    assert best_model is not None and best_model_dev is not None
    # Evaluate frozen selected model ONCE on untouched test!
    ridge_test = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=_make_ridge_signal(best_model),
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=1.0,
    )
    ridge_stress = simulate_causal_segment(
        bars,
        eval_start=split.test_start,
        eval_end=split.test_end,
        market=mkt,
        timeframe=tf_norm,
        signal_fn=_make_ridge_signal(best_model),
        initial_capital=initial_capital,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        cost_multiplier=stress_multiplier,
    )
    ridge_ok, ridge_reasons = _gate_decision(best_model_dev["validation"], ridge_test, ridge_stress)
    candidates.append(
        {
            "candidate_id": "axiom_linear_lagged",
            "family": "AXIOM_DECISION_MODEL",
            "uses_kronos": False,
            "parameters": best_model.to_artifact_dict(),
            **best_model_dev,
            "untouched_test": ridge_test,
            "stressed_test": ridge_stress,
            "promotion_eligible": ridge_ok,
            "rejection_reasons": ridge_reasons,
        }
    )

    # 5. Kronos-Assisted Strategy (Paired ablation against Kronos-free model; NEVER mocks Kronos!)
    _step("KRONOS_ABLATION", 85, "Checking Kronos availability for paired causal Kronos-assisted evaluation")
    kronos_entry: dict[str, Any]
    k_status = kronos_provider.status() if kronos_provider is not None else {"provider_state": "NOT_INSTALLED"}
    if not include_kronos:
        kronos_entry = {
            "candidate_id": "kronos_ensemble",
            "family": "KRONOS_ASSISTED",
            "uses_kronos": True,
            "status": "SKIPPED_NOT_REQUESTED",
            "skip_reason": "include_kronos=false in research configuration (baseline/Kronos-free evaluation completed).",
            "promotion_eligible": False,
            "rejection_reasons": ["Kronos ablation not enabled for this run"],
        }
    elif kronos_provider is None or k_status.get("provider_state") not in {"READY", "STALE"}:
        kronos_entry = {
            "candidate_id": "kronos_ensemble",
            "family": "KRONOS_ASSISTED",
            "uses_kronos": True,
            "status": "SKIPPED_KRONOS_UNAVAILABLE",
            "skip_reason": (
                f"Kronos provider is {k_status.get('provider_state', 'NOT_INSTALLED')} "
                f"({k_status.get('summary', 'weights/runtime unavailable')}). "
                "Fail-closed: AXIOM never substitutes mock forecasts in research runs."
            ),
            "promotion_eligible": False,
            "rejection_reasons": [f"Kronos {k_status.get('provider_state', 'NOT_INSTALLED')}"],
        }
    else:
        # Genuine Kronos causal evaluation with stride caching (queries strictly causal prefix!)
        forecast_cache: dict[str, dict[str, Any]] = {}

        def _make_kronos_sig() -> Callable[[list[Candle]], int]:
            state = {"last_k_ok": 0, "steps": 0}

            def _ksig(prefix: list[Candle]) -> int:
                if len(prefix) < max(MIN_LOOKBACK_BARS, 32):
                    return 0
                base_pred = best_model.predict_expected_return(compute_causal_feature_vector(prefix))
                ts_key = prefix[-1].timestamp.isoformat()
                if ts_key not in forecast_cache and state["steps"] % 5 == 0:
                    try:
                        forecast_cache[ts_key] = kronos_provider.predict(
                            market=mkt,
                            symbol=str(dataset_meta.get("symbol") or "RESEARCH"),
                            timeframe=tf_norm,
                            bars=prefix[-128:],
                            horizon=5,
                            samples=4,
                            require_fresh=False,
                            allow_when_toggle_off=True,
                        )
                    except KronosUnavailableError:
                        state["last_k_ok"] = 0
                        state["steps"] += 1
                        return 0
                state["steps"] += 1
                fc = forecast_cache.get(ts_key)
                if fc is not None:
                    state["last_k_ok"] = 1 if (fc["up_probability"] >= 0.55 and fc["expected_return_pct"] > 0.0) else 0
                return 1 if (base_pred > best_model.entry_threshold and state["last_k_ok"] == 1) else 0

            return _ksig

        k_dev = _eval_all_splits(_make_kronos_sig)
        k_test = simulate_causal_segment(
            bars,
            eval_start=split.test_start,
            eval_end=split.test_end,
            market=mkt,
            timeframe=tf_norm,
            signal_fn=_make_kronos_sig(),
            initial_capital=initial_capital,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            cost_multiplier=1.0,
        )
        k_stress = simulate_causal_segment(
            bars,
            eval_start=split.test_start,
            eval_end=split.test_end,
            market=mkt,
            timeframe=tf_norm,
            signal_fn=_make_kronos_sig(),
            initial_capital=initial_capital,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            cost_multiplier=stress_multiplier,
        )
        k_ok, k_reasons = _gate_decision(k_dev["validation"], k_test, k_stress)
        if k_dev["validation"]["sharpe"] <= best_model_dev["validation"]["sharpe"]:
            k_ok = False
            k_reasons.append(
                f"Kronos-assisted validation Sharpe ({k_dev['validation']['sharpe']:.2f}) did not beat Kronos-free AXIOM Ridge ({best_model_dev['validation']['sharpe']:.2f})"
            )
        kronos_entry = {
            "candidate_id": "kronos_ensemble",
            "family": "KRONOS_ASSISTED",
            "uses_kronos": True,
            "status": "EVALUATED_GENUINE_KRONOS",
            "parameters": {
                "kronos_manifest_sha256": (k_status.get("kronos_engine") or {}).get("manifest_sha256"),
                "up_probability_min": 0.55,
                "base_model": "axiom_linear_lagged",
            },
            **k_dev,
            "untouched_test": k_test,
            "stressed_test": k_stress,
            "promotion_eligible": k_ok,
            "rejection_reasons": k_reasons,
        }
    candidates.append(kronos_entry)

    _step("COMPLETED", 100, "Finalizing run manifest and cryptographic fingerprint")
    manifest_core = {
        "schema": "axiom.model_lab.research_run.v1",
        "paper_and_research_only": True,
        "live_authorization_granted": False,
        "profitability_guarantee": "NONE — historical simulation never guarantees future paper or live returns.",
        "market": mkt,
        "currency": "INR" if mkt == "NSE" else "USD",
        "timeframe": tf_norm,
        "dataset": {
            "dataset_id": dataset_meta.get("dataset_id"),
            "symbol": dataset_meta.get("symbol"),
            "sha256": dataset_meta.get("sha256"),
            "source_type": dataset_meta.get("source_type"),
            "real_market_data": bool(dataset_meta.get("real_market_data", False)),
            "survivorship_aware": bool(dataset_meta.get("survivorship_aware", False)),
            "corporate_action_adjustment": dataset_meta.get("corporate_action_adjustment"),
            "bar_count": len(bars),
        },
        "splits": {
            "train": {
                "start_idx": split.train_start,
                "end_idx": split.train_end,
                "bars": split.train_end - split.train_start,
                "start_ts": bars[split.train_start].timestamp.isoformat(),
                "end_ts": bars[split.train_end - 1].timestamp.isoformat(),
            },
            "embargo_1_bars": split.embargo_bars,
            "validation": {
                "start_idx": split.val_start,
                "end_idx": split.val_end,
                "bars": split.val_end - split.val_start,
                "start_ts": bars[split.val_start].timestamp.isoformat(),
                "end_ts": bars[split.val_end - 1].timestamp.isoformat(),
            },
            "embargo_2_bars": split.embargo_bars,
            "untouched_test": {
                "start_idx": split.test_start,
                "end_idx": split.test_end,
                "bars": split.test_end - split.test_start,
                "start_ts": bars[split.test_start].timestamp.isoformat(),
                "end_ts": bars[split.test_end - 1].timestamp.isoformat(),
                "selection_touched_test": False,
            },
        },
        "cost_assumptions": {
            "spread_bps": spread_bps,
            "slippage_bps": slippage_bps,
            "stress_multiplier": stress_multiplier,
            "min_trades": min_trades,
            "fee_schedule": "NSE_DELIVERY_STT_GST_SEBI_STAMP" if mkt == "NSE" else "US_SEC_FINRA_TAF",
        },
        "candidates": candidates,
    }
    digest = hashlib.sha256(
        json.dumps(manifest_core, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        **manifest_core,
        "manifest_sha256": digest,
        "completed_at": datetime.now(UTC).isoformat(),
    }
