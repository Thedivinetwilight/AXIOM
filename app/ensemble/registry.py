"""Configurable Strategy Registry, Completed-Bar Validator & Bounded Parallel Evaluator.

Supports evaluating hundreds or thousands of parameterized strategy variants across
multi-market watchlists without creating unbounded threads, making redundant provider
API calls, or blocking the desktop terminal UI.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from app.data.models import TIMEFRAMES, Candle, parse_timestamp

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1D": 86400,
}

STRATEGY_FAMILIES: dict[str, dict[str, Any]] = {
    "sma_crossover": {
        "version": "1.0.0",
        "description": "Fast/slow simple moving-average trend crossover with minimum edge filter.",
        "default_params": {"fast": 10, "slow": 30, "min_edge_bps": 8.0},
    },
    "ema_crossover": {
        "version": "1.0.0",
        "description": "Exponential moving-average momentum crossover gated by ATR-normalized separation.",
        "default_params": {"fast": 8, "slow": 21, "min_edge_bps": 8.0},
    },
    "rsi_reversion": {
        "version": "1.0.0",
        "description": "Wilder RSI mean-reversion buying oversold pullbacks and exiting overbought extensions.",
        "default_params": {"period": 14, "oversold": 30.0, "overbought": 70.0},
    },
    "bollinger_reversion": {
        "version": "1.0.0",
        "description": "Bollinger-band z-score mean reversion toward rolling equilibrium.",
        "default_params": {"period": 20, "num_std": 2.0},
    },
    "donchian_breakout": {
        "version": "1.0.0",
        "description": "Prior N-bar Donchian channel high/low breakout with ATR stop sizing.",
        "default_params": {"lookback": 20, "atr_stop_mult": 2.0},
    },
    "macd_momentum": {
        "version": "1.0.0",
        "description": "MACD histogram impulse crossover normalized by price volatility.",
        "default_params": {"fast": 12, "slow": 26, "signal": 9},
    },
    "vwap_zscore_reversion": {
        "version": "1.0.0",
        "description": "Rolling volume-weighted price deviation z-score mean reversion.",
        "default_params": {"window": 20, "z_entry": 1.75},
    },
    "volatility_regime_trend": {
        "version": "1.0.0",
        "description": "Volatility-filtered trend follower active only below annualized vol ceiling.",
        "default_params": {"fast": 10, "slow": 40, "max_bar_vol_pct": 2.5},
    },
}


class BarValidationError(ValueError):
    """Raised when input candles fail strict completed-bar validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_timeframe(timeframe: str) -> str:
    raw = (timeframe or "").strip()
    norm = "1D" if raw.lower() == "1d" else raw
    if norm not in TIMEFRAMES:
        raise BarValidationError("INVALID_TIMEFRAME", f"Unsupported timeframe {timeframe!r}")
    return norm


def canonical_variant_signature(family: str, params: dict[str, Any]) -> str:
    fam = family.strip().lower()
    clean_params = {
        str(k): (round(float(v), 6) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
        for k, v in sorted(params.items())
    }
    payload = json.dumps({"family": fam, "params": clean_params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class StrategyVariantSpec:
    variant_id: str
    family: str
    params: dict[str, Any]
    version: str = "1.0.0"
    enabled: bool = True
    canonical_signature: str = ""
    duplicate_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategySignalRecord:
    strategy_id: str
    family: str
    version: str
    canonical_signature: str
    market: str
    symbol: str
    timeframe: str
    bar_timestamp: str
    evaluated_at: str
    direction: int  # +1 BUY, -1 SELL/EXIT, 0 HOLD
    action: str  # BUY | SELL | HOLD
    raw_score: float  # [-1.0, +1.0], NEVER treated as calibrated probability
    stop_distance_pct: float
    rationale: str
    inputs: dict[str, Any]
    status: str  # EVALUATED | DUPLICATE_SKIPPED | DATA_REJECTED | DISABLED | ERROR
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_completed_bars(
    bars: Iterable[Candle],
    *,
    expected_market: str,
    expected_symbol: str,
    timeframe: str,
    now_utc: datetime | None = None,
    min_bars: int = 35,
    require_fresh_bars: bool = False,
    max_bar_age_seconds: float | None = None,
    max_intraday_gap_bars: int = 12,
) -> tuple[list[Candle], dict[str, Any]]:
    """Validate and filter to strictly completed, chronological, finite OHLCV bars.

    Rejects:
    - Missing/empty or insufficient history (< min_bars)
    - Timezone-naive, duplicated, or out-of-order timestamps
    - Mismatched symbol/market tags on candles
    - Non-finite or invalid OHLCV geometry
    - Unclosed current bar (dropped if bar_close_time > now_utc; rejected if too few remain)
    - Stale latest bar when require_fresh_bars=True
    - Excessive unexplained intra-session missing bar gaps when max_intraday_gap_bars > 0
    """
    tf = normalize_timeframe(timeframe)
    step_s = TIMEFRAME_SECONDS[tf]
    mkt = (expected_market or "").strip().upper()
    if mkt not in {"NSE", "NASDAQ"}:
        raise BarValidationError("INVALID_MARKET", f"Unsupported market {expected_market!r}")
    sym_expected = (expected_symbol or "").strip().upper()
    if not sym_expected:
        raise BarValidationError("MISSING_SYMBOL", "Symbol is required for bar validation")

    raw_list = list(bars or [])
    if not raw_list:
        raise BarValidationError("MISSING_BARS", f"No historical bars available for {mkt}:{sym_expected} ({tf})")

    ref_now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    completed: list[Candle] = []
    dropped_unclosed = 0
    prev_ts: datetime | None = None
    missing_gap_bars = 0

    for idx, bar in enumerate(raw_list):
        ts = bar.timestamp
        if ts.tzinfo is None:
            raise BarValidationError("NAIVE_TIMESTAMP", f"Bar {idx} has timezone-naive timestamp")
        ts_utc = ts.astimezone(UTC)

        # Symbol / market mismatch check: allow exact trading symbol or NSE_EQ-... display_symbol
        bar_sym = (bar.symbol or "").strip().upper()
        if bar_sym:
            if mkt == "NASDAQ" and ("NSE_" in bar_sym or "BSE_" in bar_sym):
                raise BarValidationError(
                    "MISMATCHED_MARKET_DATA",
                    f"Bar symbol {bar_sym!r} belongs to Indian exchange, expected NASDAQ:{sym_expected}",
                )
            if (
                mkt == "NSE"
                and bar_sym != sym_expected
                and not bar_sym.startswith(("NSE_", "DEMO_"))
                and sym_expected not in bar_sym
            ):
                raise BarValidationError(
                    "MISMATCHED_SYMBOL_DATA",
                    f"Bar symbol {bar_sym!r} does not match expected {mkt}:{sym_expected}",
                )
            if mkt == "NASDAQ" and bar_sym != sym_expected and not bar_sym.startswith("DEMO_"):
                raise BarValidationError(
                    "MISMATCHED_SYMBOL_DATA",
                    f"Bar symbol {bar_sym!r} does not match expected NASDAQ:{sym_expected}",
                )

        if prev_ts is not None:
            if ts_utc == prev_ts:
                raise BarValidationError(
                    "DUPLICATE_BAR_TIMESTAMP",
                    f"Duplicate bar timestamp {ts_utc.isoformat()} in {mkt}:{sym_expected}",
                )
            if ts_utc < prev_ts:
                raise BarValidationError(
                    "OUT_OF_ORDER_BARS",
                    f"Out-of-order bar timestamp {ts_utc.isoformat()} < {prev_ts.isoformat()}",
                )
            delta_s = (ts_utc - prev_ts).total_seconds()
            # Intra-session gap check (ignore overnight / weekend session boundaries > 14 hours)
            if step_s < 86400 and step_s * 1.5 < delta_s < 14 * 3600:
                gap_count = round(delta_s / step_s) - 1
                if gap_count > 0:
                    missing_gap_bars += gap_count
                    if max_intraday_gap_bars > 0 and gap_count > max_intraday_gap_bars:
                        raise BarValidationError(
                            "MISSING_INTRADAY_BARS",
                            f"Detected {gap_count} missing {tf} bars between {prev_ts.isoformat()} and {ts_utc.isoformat()}",
                        )
        prev_ts = ts_utc

        vals = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if not all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in vals):
            raise BarValidationError("NON_FINITE_OHLCV", f"Non-finite OHLCV value at {ts_utc.isoformat()}")
        if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
            raise BarValidationError("INVALID_OHLCV_RANGE", f"Invalid OHLCV value at {ts_utc.isoformat()}")
        if bar.high + 1e-9 < max(bar.open, bar.close) or bar.low - 1e-9 > min(bar.open, bar.close):
            raise BarValidationError("MALFORMED_CANDLE_BOUNDS", f"High/low violation at {ts_utc.isoformat()}")

        # Drop an actively forming unclosed bar whose end timestamp is still in the future
        if now_utc is not None and (ts_utc.timestamp() + step_s) > (ref_now.timestamp() + 1.0):
            dropped_unclosed += 1
            continue
        completed.append(bar)

    if len(completed) < min_bars:
        raise BarValidationError(
            "INSUFFICIENT_COMPLETED_BARS",
            f"Need >= {min_bars} completed bars for {mkt}:{sym_expected} ({tf}), got {len(completed)} (dropped_unclosed={dropped_unclosed})",
        )

    latest_ts = completed[-1].timestamp.astimezone(UTC)
    bar_age_s = max(0.0, (ref_now - latest_ts).total_seconds())
    max_age = max_bar_age_seconds if max_bar_age_seconds is not None else float(step_s * 4)
    if require_fresh_bars and bar_age_s > max_age:
        raise BarValidationError(
            "STALE_BAR_DATA",
            f"Latest completed {tf} bar ({latest_ts.isoformat()}) is {bar_age_s:.0f}s old (> {max_age:.0f}s limit)",
        )

    meta = {
        "market": mkt,
        "symbol": sym_expected,
        "timeframe": tf,
        "completed_bars": len(completed),
        "dropped_unclosed_bars": dropped_unclosed,
        "missing_intraday_bars": missing_gap_bars,
        "latest_bar_timestamp": latest_ts.isoformat(),
        "bar_age_seconds": round(bar_age_s, 2),
    }
    return completed, meta


class SharedIndicatorContext:
    """Memoizes rolling series on one validated completed-bar tail so 1,000+ strategy
    variants evaluate in microseconds without recomputing base indicators."""

    def __init__(self, bars: list[Candle], *, max_tail: int = 260) -> None:
        tail = bars[-max_tail:] if len(bars) > max_tail else list(bars)
        self.bars = tail
        self.closes = [float(b.close) for b in tail]
        self.highs = [float(b.high) for b in tail]
        self.lows = [float(b.low) for b in tail]
        self.opens = [float(b.open) for b in tail]
        self.volumes = [float(b.volume) for b in tail]
        self.latest_bar = tail[-1]
        self.latest_bar_ts_iso = tail[-1].timestamp.astimezone(UTC).isoformat()
        self.last_close = self.closes[-1]
        self._sma_cache: dict[int, tuple[float, float]] = {}
        self._ema_cache: dict[int, list[float]] = {}
        self._rsi_cache: dict[int, float] = {}
        self._std_cache: dict[int, float] = {}
        self._atr_cache: dict[int, float] = {}
        self._vwap_cache: dict[int, tuple[float, float]] = {}
        self._donchian_cache: dict[int, tuple[float, float]] = {}
        self._macd_cache: dict[tuple[int, int, int], tuple[float, float, float, float]] = {}
        self._bar_vol_20_pct: float | None = None
        self._returns: list[float] | None = None

    def returns(self) -> list[float]:
        if self._returns is None:
            rets: list[float] = []
            for i in range(1, len(self.closes)):
                prev = self.closes[i - 1]
                rets.append((self.closes[i] / prev) - 1.0 if prev > 0 else 0.0)
            self._returns = rets
        return self._returns

    def sma_pair(self, period: int) -> tuple[float, float]:
        """Return (current_sma, prev_bar_sma)."""
        p = max(2, min(period, len(self.closes) - 1))
        cached = self._sma_cache.get(p)
        if cached is not None:
            return cached
        cur = sum(self.closes[-p:]) / p
        prv = sum(self.closes[-p - 1 : -1]) / p
        self._sma_cache[p] = (cur, prv)
        return cur, prv

    def ema_series(self, period: int) -> list[float]:
        p = max(2, period)
        cached = self._ema_cache.get(p)
        if cached is not None:
            return cached
        alpha = 2.0 / (p + 1.0)
        out = [self.closes[0]]
        for c in self.closes[1:]:
            out.append(alpha * c + (1.0 - alpha) * out[-1])
        self._ema_cache[p] = out
        return out

    def rsi(self, period: int) -> float:
        p = max(2, min(period, len(self.closes) - 1))
        cached = self._rsi_cache.get(p)
        if cached is not None:
            return cached
        gains = 0.0
        losses = 0.0
        window = self.closes[-(p + 1) :]
        for i in range(1, len(window)):
            diff = window[i] - window[i - 1]
            if diff > 0:
                gains += diff
            else:
                losses -= diff
        if losses <= 1e-12:
            val = 100.0 if gains > 0 else 50.0
        else:
            rs = (gains / p) / (losses / p)
            val = 100.0 - (100.0 / (1.0 + rs))
        self._rsi_cache[p] = val
        return val

    def rolling_std(self, period: int) -> float:
        p = max(2, min(period, len(self.closes)))
        cached = self._std_cache.get(p)
        if cached is not None:
            return cached
        seg = self.closes[-p:]
        mean = sum(seg) / p
        var = sum((x - mean) ** 2 for x in seg) / max(1, p - 1)
        val = math.sqrt(max(0.0, var))
        self._std_cache[p] = val
        return val

    def atr(self, period: int = 14) -> float:
        p = max(2, min(period, len(self.closes) - 1))
        cached = self._atr_cache.get(p)
        if cached is not None:
            return cached
        trs: list[float] = []
        start = len(self.closes) - p
        for i in range(start, len(self.closes)):
            hi = self.highs[i]
            lo = self.lows[i]
            pc = self.closes[i - 1]
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
        val = sum(trs) / max(1, len(trs))
        self._atr_cache[p] = val
        return val

    def vwap_and_std(self, window: int) -> tuple[float, float]:
        w = max(3, min(window, len(self.closes)))
        cached = self._vwap_cache.get(w)
        if cached is not None:
            return cached
        c_seg = self.closes[-w:]
        v_seg = self.volumes[-w:]
        tot_v = sum(v_seg)
        vwap = (sum(c * v for c, v in zip(c_seg, v_seg, strict=False)) / tot_v) if tot_v > 0 else (sum(c_seg) / w)
        var = sum((c - vwap) ** 2 for c in c_seg) / max(1, w - 1)
        res = (vwap, math.sqrt(max(1e-12, var)))
        self._vwap_cache[w] = res
        return res

    def donchian_bounds(self, lookback: int) -> tuple[float, float]:
        lb = max(3, min(lookback, len(self.bars) - 1))
        cached = self._donchian_cache.get(lb)
        if cached is not None:
            return cached
        res = (max(self.highs[-lb - 1 : -1]), min(self.lows[-lb - 1 : -1]))
        self._donchian_cache[lb] = res
        return res

    def macd_stats(self, fast: int, slow: int, sig_p: int) -> tuple[float, float, float, float]:
        key = (fast, slow, sig_p)
        cached = self._macd_cache.get(key)
        if cached is not None:
            return cached
        ef = self.ema_series(fast)
        es = self.ema_series(slow)
        macd_line = [a - b for a, b in zip(ef, es, strict=False)]
        alpha_s = 2.0 / (sig_p + 1.0)
        sig_val = macd_line[0]
        prev_hist = 0.0
        for idx_m, m_val in enumerate(macd_line[1:], start=1):
            prev_sig = sig_val
            sig_val = alpha_s * m_val + (1.0 - alpha_s) * sig_val
            if idx_m == len(macd_line) - 2:
                prev_hist = m_val - prev_sig
        hist = macd_line[-1] - sig_val
        res = (macd_line[-1], sig_val, hist, prev_hist)
        self._macd_cache[key] = res
        return res

    def bar_vol_20_pct(self) -> float:
        if self._bar_vol_20_pct is None:
            rets = self.returns()[-20:]
            self._bar_vol_20_pct = (
                math.sqrt(sum(r * r for r in rets) / max(1, len(rets))) * 100.0 if rets else 0.0
            )
        return self._bar_vol_20_pct


def evaluate_variant_on_context(
    spec: StrategyVariantSpec,
    ctx: SharedIndicatorContext,
    *,
    market: str,
    symbol: str,
    timeframe: str,
    evaluated_at: str,
) -> StrategySignalRecord:
    """Evaluate a single parameterized strategy variant on a validated SharedIndicatorContext."""
    t0 = time.perf_counter()
    fam = spec.family
    p = spec.params
    close = ctx.last_close
    atr14 = ctx.atr(14)
    atr_pct = max(0.25, min(12.0, (atr14 / close) * 100.0 if close > 0 else 1.0))
    stop_dist_pct = round(max(0.35, min(10.0, atr_pct * float(p.get("atr_stop_mult", 1.8)))), 4)

    direction = 0
    raw_score = 0.0
    rationale = "Neutral completed-bar state"
    inputs_summary: dict[str, Any] = {
        "close": round(close, 4),
        "atr14": round(atr14, 4),
        "bars": len(ctx.bars),
    }

    if fam == "sma_crossover":
        fast = int(p.get("fast", 10))
        slow = max(fast + 2, int(p.get("slow", 30)))
        min_bps = float(p.get("min_edge_bps", 8.0))
        f_cur, _ = ctx.sma_pair(fast)
        s_cur, _ = ctx.sma_pair(slow)
        spread_bps = ((f_cur / s_cur) - 1.0) * 10_000.0 if s_cur > 0 else 0.0
        raw_score = max(-1.0, min(1.0, spread_bps / max(25.0, atr_pct * 80.0)))
        if spread_bps >= min_bps:
            direction = 1
            rationale = f"SMA({fast})>{slow} by +{spread_bps:.1f} bps (>= {min_bps:.1f} bps)"
        elif spread_bps <= -min_bps:
            direction = -1
            rationale = f"SMA({fast})<{slow} by {spread_bps:.1f} bps (<= -{min_bps:.1f} bps)"
        inputs_summary.update({"sma_fast": round(f_cur, 4), "sma_slow": round(s_cur, 4), "spread_bps": round(spread_bps, 2)})

    elif fam == "ema_crossover":
        fast = int(p.get("fast", 8))
        slow = max(fast + 2, int(p.get("slow", 21)))
        min_bps = float(p.get("min_edge_bps", 8.0))
        ef = ctx.ema_series(fast)[-1]
        es = ctx.ema_series(slow)[-1]
        spread_bps = ((ef / es) - 1.0) * 10_000.0 if es > 0 else 0.0
        raw_score = max(-1.0, min(1.0, spread_bps / max(25.0, atr_pct * 75.0)))
        if spread_bps >= min_bps:
            direction = 1
            rationale = f"EMA({fast}) above EMA({slow}) (+{spread_bps:.1f} bps)"
        elif spread_bps <= -min_bps:
            direction = -1
            rationale = f"EMA({fast}) below EMA({slow}) ({spread_bps:.1f} bps)"
        inputs_summary.update({"ema_fast": round(ef, 4), "ema_slow": round(es, 4), "spread_bps": round(spread_bps, 2)})

    elif fam == "rsi_reversion":
        period = int(p.get("period", 14))
        oversold = float(p.get("oversold", 30.0))
        overbought = float(p.get("overbought", 70.0))
        r_val = ctx.rsi(period)
        raw_score = max(-1.0, min(1.0, (50.0 - r_val) / 35.0))
        if r_val <= oversold:
            direction = 1
            rationale = f"RSI({period})={r_val:.1f} <= oversold {oversold:.1f}"
        elif r_val >= overbought:
            direction = -1
            rationale = f"RSI({period})={r_val:.1f} >= overbought {overbought:.1f}"
        inputs_summary.update({"rsi": round(r_val, 2), "oversold": oversold, "overbought": overbought})

    elif fam == "bollinger_reversion":
        period = int(p.get("period", 20))
        num_std = float(p.get("num_std", 2.0))
        mid, _ = ctx.sma_pair(period)
        sd = max(1e-9, ctx.rolling_std(period))
        z = (close - mid) / sd
        raw_score = max(-1.0, min(1.0, -z / max(1.0, num_std * 1.5)))
        if z <= -num_std:
            direction = 1
            rationale = f"Bollinger z={z:.2f} <= -{num_std:.2f} (lower band pierce)"
        elif z >= num_std:
            direction = -1
            rationale = f"Bollinger z={z:.2f} >= +{num_std:.2f} (upper band extension)"
        inputs_summary.update({"bb_mid": round(mid, 4), "bb_std": round(sd, 4), "z_score": round(z, 3)})

    elif fam == "donchian_breakout":
        lookback = max(3, min(int(p.get("lookback", 20)), len(ctx.bars) - 1))
        prior_hi, prior_lo = ctx.donchian_bounds(lookback)
        ch_width = max(1e-9, prior_hi - prior_lo)
        pos = ((close - prior_lo) / ch_width) * 2.0 - 1.0
        raw_score = max(-1.0, min(1.0, pos))
        if close > prior_hi:
            direction = 1
            rationale = f"Close {close:.2f} broke {lookback}-bar Donchian high {prior_hi:.2f}"
        elif close < prior_lo:
            direction = -1
            rationale = f"Close {close:.2f} broke {lookback}-bar Donchian low {prior_lo:.2f}"
        inputs_summary.update({"donchian_high": round(prior_hi, 4), "donchian_low": round(prior_lo, 4)})

    elif fam == "macd_momentum":
        fast = int(p.get("fast", 12))
        slow = max(fast + 2, int(p.get("slow", 26)))
        sig_p = max(2, int(p.get("signal", 9)))
        macd_last, sig_val, hist, prev_hist = ctx.macd_stats(fast, slow, sig_p)
        norm_hist = hist / max(1e-6, atr14)
        raw_score = max(-1.0, min(1.0, norm_hist))
        if hist > 0 and (prev_hist <= 0 or norm_hist > 0.15):
            direction = 1
            rationale = f"MACD({fast},{slow},{sig_p}) bullish histogram {hist:.4f}"
        elif hist < 0 and (prev_hist >= 0 or norm_hist < -0.15):
            direction = -1
            rationale = f"MACD({fast},{slow},{sig_p}) bearish histogram {hist:.4f}"
        inputs_summary.update({"macd": round(macd_last, 4), "signal": round(sig_val, 4), "hist": round(hist, 4)})

    elif fam == "vwap_zscore_reversion":
        win = int(p.get("window", 20))
        z_entry = float(p.get("z_entry", 1.75))
        vwap, vsd = ctx.vwap_and_std(win)
        z = (close - vwap) / max(1e-9, vsd)
        raw_score = max(-1.0, min(1.0, -z / max(1.0, z_entry * 1.5)))
        if z <= -z_entry:
            direction = 1
            rationale = f"VWAP({win}) z={z:.2f} <= -{z_entry:.2f}"
        elif z >= z_entry:
            direction = -1
            rationale = f"VWAP({win}) z={z:.2f} >= +{z_entry:.2f}"
        inputs_summary.update({"vwap": round(vwap, 4), "vwap_z": round(z, 3)})

    elif fam == "volatility_regime_trend":
        fast = int(p.get("fast", 10))
        slow = max(fast + 2, int(p.get("slow", 40)))
        max_vol = float(p.get("max_bar_vol_pct", 2.5))
        bar_vol_pct = ctx.bar_vol_20_pct()
        f_cur, _ = ctx.sma_pair(fast)
        s_cur, _ = ctx.sma_pair(slow)
        spread_bps = ((f_cur / s_cur) - 1.0) * 10_000.0 if s_cur > 0 else 0.0
        if bar_vol_pct <= max_vol:
            raw_score = max(-1.0, min(1.0, spread_bps / 80.0))
            if spread_bps >= 10.0:
                direction = 1
                rationale = f"Low-vol regime ({bar_vol_pct:.2f}% <= {max_vol:.2f}%) trend up +{spread_bps:.1f} bps"
            elif spread_bps <= -10.0:
                direction = -1
                rationale = f"Low-vol regime ({bar_vol_pct:.2f}% <= {max_vol:.2f}%) trend down {spread_bps:.1f} bps"
        else:
            rationale = f"Bar vol {bar_vol_pct:.2f}% > {max_vol:.2f}% ceiling; standing aside"
        inputs_summary.update({"bar_vol_pct": round(bar_vol_pct, 3), "spread_bps": round(spread_bps, 2)})

    else:
        raise ValueError(f"Unknown strategy family {fam!r}")

    action = "BUY" if direction > 0 else ("SELL" if direction < 0 else "HOLD")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return StrategySignalRecord(
        strategy_id=spec.variant_id,
        family=fam,
        version=spec.version,
        canonical_signature=spec.canonical_signature,
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        bar_timestamp=ctx.latest_bar_ts_iso,
        evaluated_at=evaluated_at,
        direction=direction,
        action=action,
        raw_score=round(raw_score, 5),
        stop_distance_pct=stop_dist_pct,
        rationale=rationale,
        inputs=inputs_summary,
        status="EVALUATED",
        latency_ms=round(elapsed_ms, 4),
    )


def build_preset_variants(preset: str = "compact", *, enabled_families: list[str] | None = None) -> list[StrategyVariantSpec]:
    """Deterministically generate a parameterized library of strategy variants.

    Presets:
    - 'compact':   ~24 variants across the 8 families (fast interactive default)
    - 'standard':  ~96 variants
    - 'large_256': 256 variants
    - 'massive_1024': 1,024 variants (stress/scale validation)
    """
    fam_filter = {f.strip().lower() for f in enabled_families} if enabled_families else set(STRATEGY_FAMILIES.keys())
    fam_filter = fam_filter & set(STRATEGY_FAMILIES.keys())
    if not fam_filter:
        fam_filter = set(STRATEGY_FAMILIES.keys())

    preset_norm = (preset or "compact").strip().lower()
    target_counts = {
        "compact": 24,
        "standard": 96,
        "large_256": 256,
        "massive_1024": 1024,
    }
    target = target_counts.get(preset_norm, 24)

    candidates: list[tuple[str, dict[str, Any]]] = []
    # Rich deterministic parameter grids per family (>= 1,300 unique candidates across 8 families)
    for fast in (4, 5, 7, 8, 10, 12, 15, 18, 20, 25, 30):
        for slow in (18, 22, 28, 35, 42, 50, 60, 75, 90, 110, 130):
            if slow >= fast + 8:
                for edge in (4.0, 8.0, 14.0, 20.0):
                    if "sma_crossover" in fam_filter:
                        candidates.append(("sma_crossover", {"fast": fast, "slow": slow, "min_edge_bps": edge}))
                    if "ema_crossover" in fam_filter:
                        candidates.append(("ema_crossover", {"fast": fast, "slow": slow, "min_edge_bps": edge}))

    if "rsi_reversion" in fam_filter:
        for p in (6, 8, 10, 12, 14, 18, 21, 26, 32):
            for os_lvl in (20.0, 24.0, 28.0, 32.0, 36.0, 40.0):
                for ob_lvl in (60.0, 64.0, 68.0, 72.0, 76.0, 80.0):
                    candidates.append(("rsi_reversion", {"period": p, "oversold": os_lvl, "overbought": ob_lvl}))

    if "bollinger_reversion" in fam_filter:
        for p in (10, 12, 14, 16, 18, 20, 24, 28, 34, 40, 50, 60):
            for sd in (1.4, 1.6, 1.8, 2.0, 2.2, 2.4, 2.6, 2.8, 3.0, 3.2):
                candidates.append(("bollinger_reversion", {"period": p, "num_std": sd}))

    if "donchian_breakout" in fam_filter:
        for lb in (8, 10, 12, 15, 18, 20, 24, 28, 35, 42, 50, 65, 80, 100):
            for mult in (1.2, 1.5, 1.8, 2.1, 2.4, 2.8, 3.2, 3.6):
                candidates.append(("donchian_breakout", {"lookback": lb, "atr_stop_mult": mult}))

    if "macd_momentum" in fam_filter:
        for fast in (5, 6, 8, 10, 12, 15, 18):
            for slow in (18, 22, 26, 30, 36, 44):
                if slow >= fast + 8:
                    for sig in (5, 7, 9, 11, 13):
                        candidates.append(("macd_momentum", {"fast": fast, "slow": slow, "signal": sig}))

    if "vwap_zscore_reversion" in fam_filter:
        for w in (8, 10, 12, 15, 18, 20, 25, 30, 36, 44, 52, 60):
            for z in (1.1, 1.3, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25):
                candidates.append(("vwap_zscore_reversion", {"window": w, "z_entry": z}))

    if "volatility_regime_trend" in fam_filter:
        for fast in (5, 7, 10, 13, 16, 20, 24):
            for slow in (22, 30, 40, 52, 65, 80, 100):
                if slow >= fast + 10:
                    for mv in (1.0, 1.4, 1.8, 2.3, 2.8, 3.5):
                        candidates.append(("volatility_regime_trend", {"fast": fast, "slow": slow, "max_bar_vol_pct": mv}))

    # Interleave evenly across families so every preset covers all enabled families
    by_fam: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for fam, prm in candidates:
        by_fam.setdefault(fam, []).append((fam, prm))

    ordered_fams = sorted(by_fam.keys())
    selected: list[StrategyVariantSpec] = []
    seen_sigs: dict[str, str] = {}
    idx_round = 0
    while len(selected) < target:
        progressed = False
        for fam in ordered_fams:
            bucket = by_fam[fam]
            if idx_round < len(bucket):
                progressed = True
                _, prm = bucket[idx_round]
                sig = canonical_variant_signature(fam, prm)
                if sig in seen_sigs:
                    continue
                vid = f"{fam}_v{len(selected) + 1:04d}_{sig[:6]}"
                seen_sigs[sig] = vid
                selected.append(
                    StrategyVariantSpec(
                        variant_id=vid,
                        family=fam,
                        params=prm,
                        version=STRATEGY_FAMILIES[fam]["version"],
                        enabled=True,
                        canonical_signature=sig,
                        duplicate_of=None,
                    )
                )
                if len(selected) >= target:
                    break
        if not progressed:
            break
        idx_round += 1
    return selected


class StrategyRegistry:
    """Thread-safe registry of configured strategy variants with canonical duplicate tracking."""

    def __init__(self, initial_variants: list[StrategyVariantSpec] | None = None) -> None:
        self._variants: dict[str, StrategyVariantSpec] = {}
        self._sig_owner: dict[str, str] = {}
        if initial_variants is not None:
            self.replace_all(initial_variants)
        else:
            self.replace_all(build_preset_variants("compact"))

    def replace_all(self, variants: Iterable[StrategyVariantSpec | dict[str, Any]]) -> dict[str, Any]:
        new_map: dict[str, StrategyVariantSpec] = {}
        sig_owner: dict[str, str] = {}
        duplicates = 0
        for raw in variants:
            if isinstance(raw, StrategyVariantSpec):
                vid = raw.variant_id.strip()
                fam = raw.family.strip().lower()
                prm = dict(raw.params)
                enabled = bool(raw.enabled)
            else:
                vid = str(raw.get("variant_id") or "").strip()
                fam = str(raw.get("family") or "").strip().lower()
                prm = dict(raw.get("params") or {})
                enabled = bool(raw.get("enabled", True))
            if fam not in STRATEGY_FAMILIES:
                raise ValueError(f"Unsupported strategy family {fam!r}")
            sig = canonical_variant_signature(fam, prm)
            if not vid:
                vid = f"{fam}_{sig[:8]}"
            dup_of = sig_owner.get(sig)
            if dup_of is not None and dup_of != vid:
                duplicates += 1
            else:
                sig_owner[sig] = vid
            new_map[vid] = StrategyVariantSpec(
                variant_id=vid,
                family=fam,
                params=prm,
                version=STRATEGY_FAMILIES[fam]["version"],
                enabled=enabled,
                canonical_signature=sig,
                duplicate_of=dup_of if dup_of != vid else None,
            )
        self._variants = new_map
        self._sig_owner = sig_owner
        return self.summary()

    def list_variants(self, *, enabled_only: bool = False) -> list[StrategyVariantSpec]:
        items = list(self._variants.values())
        return [v for v in items if v.enabled] if enabled_only else items

    def summary(self) -> dict[str, Any]:
        all_v = list(self._variants.values())
        enabled_v = [v for v in all_v if v.enabled]
        unique_enabled = [v for v in enabled_v if v.duplicate_of is None]
        fam_counts: dict[str, int] = {}
        for v in enabled_v:
            fam_counts[v.family] = fam_counts.get(v.family, 0) + 1
        return {
            "total_variants": len(all_v),
            "enabled_variants": len(enabled_v),
            "unique_canonical_variants": len(unique_enabled),
            "duplicate_variants_detected": sum(1 for v in all_v if v.duplicate_of is not None),
            "by_family": fam_counts,
            "available_families": STRATEGY_FAMILIES,
        }


class BoundedParallelEvaluator:
    """Evaluates potentially thousands of strategy variants across instruments using
    a bounded worker pool and single-fetch SharedIndicatorContext memoization."""

    MAX_ALLOWED_WORKERS = 32

    def __init__(self, *, max_workers: int = 4, batch_timeout_seconds: float = 30.0) -> None:
        self.max_workers = max(1, min(int(max_workers), self.MAX_ALLOWED_WORKERS))
        self.batch_timeout_seconds = max(1.0, min(float(batch_timeout_seconds), 120.0))

    def configure(self, *, max_workers: int | None = None, batch_timeout_seconds: float | None = None) -> None:
        if max_workers is not None:
            self.max_workers = max(1, min(int(max_workers), self.MAX_ALLOWED_WORKERS))
        if batch_timeout_seconds is not None:
            self.batch_timeout_seconds = max(1.0, min(float(batch_timeout_seconds), 120.0))

    def evaluate_instrument(
        self,
        *,
        market: str,
        symbol: str,
        timeframe: str,
        bars: list[Candle],
        variants: list[StrategyVariantSpec],
        now_utc: datetime | None = None,
        require_fresh_bars: bool = False,
        max_bar_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Validate bars once, memoize indicators once, and evaluate all variants."""
        t0 = time.perf_counter()
        eval_ts = (now_utc or datetime.now(UTC)).astimezone(UTC).isoformat()
        validated_bars, bar_meta = validate_completed_bars(
            bars,
            expected_market=market,
            expected_symbol=symbol,
            timeframe=timeframe,
            now_utc=now_utc,
            require_fresh_bars=require_fresh_bars,
            max_bar_age_seconds=max_bar_age_seconds,
        )
        ctx = SharedIndicatorContext(validated_bars)
        records: list[StrategySignalRecord] = []
        seen_canonical: dict[str, StrategySignalRecord] = {}
        duplicates_skipped = 0

        for spec in variants:
            if not spec.enabled:
                continue
            if spec.duplicate_of is not None or spec.canonical_signature in seen_canonical:
                duplicates_skipped += 1
                records.append(
                    StrategySignalRecord(
                        strategy_id=spec.variant_id,
                        family=spec.family,
                        version=spec.version,
                        canonical_signature=spec.canonical_signature,
                        market=bar_meta["market"],
                        symbol=bar_meta["symbol"],
                        timeframe=bar_meta["timeframe"],
                        bar_timestamp=bar_meta["latest_bar_timestamp"],
                        evaluated_at=eval_ts,
                        direction=0,
                        action="HOLD",
                        raw_score=0.0,
                        stop_distance_pct=0.0,
                        rationale=f"Duplicate canonical variant of {spec.duplicate_of or seen_canonical[spec.canonical_signature].strategy_id}; deduplicated before allocation",
                        inputs={},
                        status="DUPLICATE_SKIPPED",
                        latency_ms=0.0,
                    )
                )
                continue

            rec = evaluate_variant_on_context(
                spec,
                ctx,
                market=bar_meta["market"],
                symbol=bar_meta["symbol"],
                timeframe=bar_meta["timeframe"],
                evaluated_at=eval_ts,
            )
            seen_canonical[spec.canonical_signature] = rec
            records.append(rec)

        total_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "ok": True,
            "market": bar_meta["market"],
            "symbol": bar_meta["symbol"],
            "timeframe": bar_meta["timeframe"],
            "bar_meta": bar_meta,
            "context": ctx,
            "signals": records,
            "unique_evaluated": len(seen_canonical),
            "duplicates_skipped": duplicates_skipped,
            "wall_ms": round(total_ms, 3),
        }

    def evaluate_watchlist(
        self,
        *,
        market: str,
        timeframe: str,
        bars_by_symbol: dict[str, list[Candle]],
        variants: list[StrategyVariantSpec],
        now_utc: datetime | None = None,
        require_fresh_bars: bool = False,
        max_bar_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Concurrently evaluate all watchlist instruments using at most `max_workers` threads."""
        t0 = time.perf_counter()
        symbols = sorted(bars_by_symbol.keys())
        results: dict[str, dict[str, Any]] = {}
        rejected_instruments: dict[str, dict[str, str]] = {}

        if not symbols:
            return {
                "market": market.upper(),
                "timeframe": normalize_timeframe(timeframe),
                "workers_used": 0,
                "instruments": {},
                "rejected_instruments": {},
                "total_evaluations": 0,
                "wall_ms": 0.0,
            }

        workers = min(self.max_workers, len(symbols))

        def _task(sym: str) -> tuple[str, dict[str, Any] | None, BarValidationError | Exception | None]:
            try:
                res = self.evaluate_instrument(
                    market=market,
                    symbol=sym,
                    timeframe=timeframe,
                    bars=bars_by_symbol[sym],
                    variants=variants,
                    now_utc=now_utc,
                    require_fresh_bars=require_fresh_bars,
                    max_bar_age_seconds=max_bar_age_seconds,
                )
                return sym, res, None
            except Exception as exc:  # noqa: BLE001 - captured per symbol to keep sibling symbols isolated
                return sym, None, exc

        if workers <= 1:
            for sym in symbols:
                s, res, err = _task(sym)
                if err is not None:
                    code = err.code if isinstance(err, BarValidationError) else type(err).__name__
                    rejected_instruments[s] = {"code": code, "reason": str(err)[:220]}
                elif res is not None:
                    results[s] = res
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="axiom-ens") as pool:
                futs = {sym: pool.submit(_task, sym) for sym in symbols}
                for sym, fut in futs.items():
                    try:
                        s, res, err = fut.result(timeout=self.batch_timeout_seconds)
                        if err is not None:
                            code = err.code if isinstance(err, BarValidationError) else type(err).__name__
                            rejected_instruments[s] = {"code": code, "reason": str(err)[:220]}
                        elif res is not None:
                            results[s] = res
                    except FuturesTimeoutError:
                        rejected_instruments[sym] = {
                            "code": "EVALUATION_TIMEOUT",
                            "reason": f"Evaluation timed out after {self.batch_timeout_seconds:.1f}s",
                        }

        total_evals = sum(len(r["signals"]) for r in results.values())
        wall_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "market": market.upper(),
            "timeframe": normalize_timeframe(timeframe),
            "workers_used": workers,
            "max_workers_configured": self.max_workers,
            "instruments": results,
            "rejected_instruments": rejected_instruments,
            "total_evaluations": total_evals,
            "wall_ms": round(wall_ms, 3),
        }


def parse_bar_timestamp_safe(ts_str: str) -> datetime:
    return parse_timestamp(ts_str)
