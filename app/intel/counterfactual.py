"""Counterfactual simulation over recorded bars (master spec §99).

Answers "what if?" for important recommendation candidates against
OBSERVED recorded closes:

* different entry timing (enter earlier/later by N bars);
* different sizing (a multiplier on the position size);
* no-trade (skip the entry entirely);
* delayed entry / alternative entry — same machinery, different offset.

Everything produced here is labelled ``SIMULATED`` — a counterfactual is
never observed reality, and the output says so in every row. The simulator
is pure arithmetic on the recorded series: no forecasts, no fabrication;
when the recorded series cannot support the requested window the call
raises ``ValueError`` instead of guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LABEL = "SIMULATED"


def _validate_series(closes: list[float]) -> None:
    if len(closes) < 2:
        raise ValueError("counterfactual simulation needs at least two "
                         "recorded closes")
    for i, close in enumerate(closes):
        if not isinstance(close, (int, float)) or isinstance(close, bool) \
                or not math.isfinite(close) or close <= 0:
            raise ValueError(f"recorded close at index {i} is not a "
                             f"positive finite number")


@dataclass(frozen=True)
class VariantResult:
    name: str
    entry_index: int | None       # None == NO_TRADE
    size: float
    horizon: int
    entry_price: float | None
    exit_price: float | None
    return_pct: float             # 0.0 for NO_TRADE
    max_drawdown_pct: float       # worst peak-to-trough within the window
    label: str                    # always SIMULATED

    def to_dict(self) -> dict:
        return {"name": self.name, "entry_index": self.entry_index,
                "size": self.size, "horizon": self.horizon,
                "entry_price": self.entry_price,
                "exit_price": self.exit_price,
                "return_pct": round(self.return_pct, 6),
                "max_drawdown_pct": round(self.max_drawdown_pct, 6),
                "label": self.label}


class CounterfactualSimulator:
    """Deterministic what-if arithmetic on a recorded close series."""

    def __init__(self, closes: list[float]):
        _validate_series(closes)
        self.closes = [float(c) for c in closes]

    # ------------------------------------------------------------ variants --
    def simulate_entry(self, name: str, entry_index: int, *,
                       horizon: int = 5, size: float = 1.0) -> VariantResult:
        if not isinstance(horizon, int) or horizon < 1:
            raise ValueError("horizon must be a positive integer of bars")
        if size <= 0 or not math.isfinite(size):
            raise ValueError("size must be a positive finite multiplier")
        n = len(self.closes)
        if not 0 <= entry_index < n:
            raise ValueError(f"entry_index {entry_index} outside recorded "
                             f"range 0..{n - 1}")
        if entry_index + horizon >= n:
            raise ValueError(
                f"recorded history ends before the {horizon}-bar outcome "
                f"window after entry {entry_index} — cannot simulate "
                f"without fabricating bars")
        window = self.closes[entry_index:entry_index + horizon + 1]
        entry = window[0]
        exit_price = window[-1]
        ret = (exit_price / entry - 1.0) * size
        peak = window[0]
        worst = 0.0
        for close in window[1:]:
            peak = max(peak, close)
            worst = min(worst, (close / peak - 1.0) * size)
        return VariantResult(name=name, entry_index=entry_index, size=size,
                             horizon=horizon, entry_price=entry,
                             exit_price=exit_price, return_pct=ret * 100.0,
                             max_drawdown_pct=worst * 100.0, label=LABEL)

    def no_trade(self, name: str = "no-trade") -> VariantResult:
        return VariantResult(name=name, entry_index=None, size=0.0,
                             horizon=0, entry_price=None, exit_price=None,
                             return_pct=0.0, max_drawdown_pct=0.0,
                             label=LABEL)

    # ----------------------------------------------------------- scenarios --
    def compare(self, base_entry: int, *, horizon: int = 5, size: float = 1.0,
                timing_offsets: tuple[int, ...] = (-2, -1, 1, 2),
                sizes: tuple[float, ...] = (0.5, 2.0)) -> dict:
        """Standard what-if battery around one proposed entry."""
        baseline = self.simulate_entry("baseline", base_entry,
                                       horizon=horizon, size=size)
        variants = [baseline, self.no_trade()]
        for offset in timing_offsets:
            idx = base_entry + offset
            if 0 <= idx < len(self.closes) and idx + horizon < len(self.closes):
                variants.append(self.simulate_entry(
                    f"entry {'+' if offset > 0 else ''}{offset} bars",
                    idx, horizon=horizon, size=size))
        for alt_size in sizes:
            variants.append(self.simulate_entry(
                f"size x{alt_size:g}", base_entry, horizon=horizon,
                size=alt_size))
        best = max(variants, key=lambda v: v.return_pct)
        return {"base_entry": base_entry, "horizon": horizon,
                "recorded_bars": len(self.closes),
                "variants": [v.to_dict() for v in variants],
                "best_variant": best.name,
                "label": LABEL,
                "note": "counterfactual arithmetic on recorded closes — "
                        "simulations, not observed outcomes"}
