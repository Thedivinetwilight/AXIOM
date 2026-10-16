"""MAE / MFE Exit Intelligence — uses existing trade history.

For every completed trade, record MAE, MFE, time to MAE/MFE, max unrealized profit/loss,
time in trade. Use to improve stop/profit/trailing/time exits.

All changes must pass chronological/OOS/robustness; hard stops remain authoritative.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List


@dataclass
class ExcursionRecord:
    trade_id: str
    strategy: str
    entry_price: float
    exit_price: float
    quantity: float
    side: str
    entry_ts: str
    exit_ts: str
    mae: float  # max adverse (negative)
    mfe: float  # max favorable (positive)
    time_to_mae: int | None = None  # bars
    time_to_mfe: int | None = None
    max_unrealized_profit: float = 0.0
    max_unrealized_loss: float = 0.0
    time_in_trade: int = 0
    bars: List[dict] = field(default_factory=list)  # optional OHLC for calc


class MAEMFEAnalyzer:
    def __init__(self):
        self._records: List[ExcursionRecord] = []

    def record_trade(self, trade: dict, bars: List[dict] | None = None) -> ExcursionRecord | None:
        # trade expected keys: id, strategy, entry, exit, quantity, entry_timestamp, exit_timestamp
        try:
            tid = str(trade.get("id") or trade.get("trade_id") or "")
            if not tid:
                return None
            strat = str(trade.get("strategy") or "unknown")
            entry = float(trade.get("entry") or trade.get("entry_price") or 0)
            exit_p = float(trade.get("exit") or trade.get("exit_price") or entry)
            qty = float(trade.get("quantity") or 0)
            if entry <= 0 or qty == 0 or math.isnan(entry) or math.isinf(entry):
                return None
            # If bars provided, compute true MAE/MFE from high/low
            mae = 0.0
            mfe = 0.0
            max_profit = 0.0
            max_loss = 0.0
            time_to_mae = None
            time_to_mfe = None
            time_in = 0
            if bars:
                # bars: list of dicts with high, low, close, timestamp
                # For long: adverse = (low - entry)/entry, favorable = (high - entry)/entry
                for i, b in enumerate(bars):
                    try:
                        high = float(b.get("high") or b.get("close") or entry)
                        low = float(b.get("low") or b.get("close") or entry)
                        if math.isnan(high) or math.isnan(low) or math.isinf(high) or math.isinf(low):
                            continue
                        # long only for now
                        adverse = (low - entry) / entry
                        favorable = (high - entry) / entry
                        if adverse < mae:
                            mae = adverse
                            time_to_mae = i
                        if favorable > mfe:
                            mfe = favorable
                            time_to_mfe = i
                        # unrealized
                        profit = max(0, favorable) * qty * entry
                        loss = min(0, adverse) * qty * entry
                        max_profit = max(max_profit, profit)
                        max_loss = min(max_loss, loss)
                    except Exception:
                        continue
                time_in = len(bars)
            else:
                # No bars: approximate from entry/exit
                ret = (exit_p - entry) / entry if entry else 0
                if ret < 0:
                    mae = ret
                    max_loss = ret * qty * entry
                else:
                    mfe = ret
                    max_profit = ret * qty * entry
                time_in = 1

            rec = ExcursionRecord(
                trade_id=tid,
                strategy=strat,
                entry_price=entry,
                exit_price=exit_p,
                quantity=qty,
                side=str(trade.get("side") or "BUY"),
                entry_ts=str(trade.get("entry_timestamp") or ""),
                exit_ts=str(trade.get("exit_timestamp") or ""),
                mae=float(mae),
                mfe=float(mfe),
                time_to_mae=time_to_mae,
                time_to_mfe=time_to_mfe,
                max_unrealized_profit=float(max_profit),
                max_unrealized_loss=float(max_loss),
                time_in_trade=int(time_in),
                bars=bars or [],
            )
            self._records.append(rec)
            if len(self._records) > 2000:
                self._records = self._records[-2000:]
            return rec
        except Exception:
            return None

    def stats(self, strategy: str | None = None) -> dict:
        recs = [r for r in self._records if strategy is None or r.strategy == strategy]
        if not recs:
            return {"trades": 0}
        avg_mae = sum(r.mae for r in recs) / len(recs)
        avg_mfe = sum(r.mfe for r in recs) / len(recs)
        avg_time = sum(r.time_in_trade for r in recs) / len(recs)
        # Stop suggestion: 90th percentile of MAE (adverse) * 1.1, but not tighter than hard stop
        sorted_mae = sorted([abs(r.mae) for r in recs])
        # 90th percentile
        idx = int(len(sorted_mae) * 0.9)
        idx = min(idx, len(sorted_mae) - 1)
        p90_mae = sorted_mae[idx] if sorted_mae else 0.05
        suggested_stop = min(0.1, max(0.02, p90_mae * 1.1))
        # Profit-taking: median MFE
        sorted_mfe = sorted([r.mfe for r in recs])
        median_mfe = sorted_mfe[len(sorted_mfe)//2] if sorted_mfe else 0.05
        suggested_tp = max(0.03, median_mfe * 0.8)

        return {
            "trades": len(recs),
            "avg_mae": avg_mae,
            "avg_mfe": avg_mfe,
            "avg_time_in_trade": avg_time,
            "suggested_stop": suggested_stop,
            "suggested_take_profit": suggested_tp,
            "max_mfe": max(r.mfe for r in recs) if recs else 0,
            "min_mae": min(r.mae for r in recs) if recs else 0,
        }

    def should_adjust_exits(self, strategy: str | None = None, min_trades: int = 10) -> tuple[bool, dict]:
        s = self.stats(strategy)
        if s["trades"] < min_trades:
            return False, {"reason": "insufficient_trades", **s}
        # Only suggest if data shows clear edge
        if s["avg_mfe"] > abs(s["avg_mae"]) * 1.5:
            return True, {"reason": "favorable_mfe", **s}
        return False, {"reason": "no_clear_edge", **s}

    def list_records(self, strategy: str | None = None, limit: int = 100) -> List[ExcursionRecord]:
        recs = [r for r in self._records if strategy is None or r.strategy == strategy]
        return recs[-limit:]
