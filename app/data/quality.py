"""OHLCV data-quality checks → VALID / WARNING / INVALID / MISSING / UNVERIFIED.

Deterministic, explainable checks over an ordered candle list:
- ohlc_relationships: high >= max(open, close), low <= min(open, close), all > 0
- ordering: strictly increasing timestamps
- duplicates: repeated timestamps
- gaps: missing intervals vs the expected cadence (bounded, business-aware
  only via cadence; exchange holidays are reported as gaps, never filled)
- jumps: suspicious close-to-close moves beyond `jump_fraction`

Nothing is ever fabricated or deleted; findings are reported as events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from app.history.store import parse_ts

CLASSIFICATIONS = ("VALID", "WARNING", "INVALID", "MISSING", "UNVERIFIED")

# Expected candle cadence per documented unit/interval.
CADENCE_MINUTES = {
    ("minutes", 1): 1, ("minutes", 2): 2, ("minutes", 3): 3, ("minutes", 5): 5,
    ("minutes", 10): 10, ("minutes", 15): 15, ("minutes", 30): 30,
    ("hours", 1): 60, ("hours", 2): 120, ("hours", 3): 180, ("hours", 4): 240,
    ("hours", 5): 300, ("days", 1): 1440, ("weeks", 1): 10080,
    ("months", 1): 43200,
}


@dataclass
class QualityReport:
    classification: str
    findings: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.classification in {"VALID", "WARNING", "MISSING"}

    def summary(self) -> dict:
        return {"classification": self.classification, "findings": self.findings}


def quality_report(candles: list[dict], *, unit: str = "minutes", interval: int = 1,
                   jump_fraction: float = 0.25, max_gap_reporting: int = 25) -> QualityReport:
    findings: list[dict] = []
    invalid = warning = missing = 0

    if not candles:
        return QualityReport("UNVERIFIED", [{"check": "empty", "detail": "no candles"}])

    previous = None
    seen_ts = set()
    duplicates = out_of_order = bad_ohlc = 0
    for candle in candles:
        o, h, low, c = (float(candle["open"]), float(candle["high"]),
                        float(candle["low"]), float(candle["close"]))
        ts = parse_ts(candle["ts"])
        if o <= 0 or h <= 0 or low <= 0 or c <= 0 or h < max(o, c) or low > min(o, c) or h < low:
            bad_ohlc += 1
        key = ts.isoformat()
        if key in seen_ts:
            duplicates += 1
        elif previous is not None and ts <= previous:
            out_of_order += 1
        seen_ts.add(key)
        previous = ts

    if bad_ohlc:
        findings.append({"check": "ohlc_relationships", "count": bad_ohlc})
        invalid += bad_ohlc
    if duplicates:
        findings.append({"check": "duplicates", "count": duplicates})
        warning += duplicates
    if out_of_order:
        findings.append({"check": "ordering", "count": out_of_order})
        invalid += out_of_order

    # Gap detection applies to intraday cadences only: daily/weekly/monthly
    # series legitimately skip weekends and holidays, and without an exchange
    # calendar those must not be reported as missing data.
    cadence = CADENCE_MINUTES.get((unit, int(interval))) if unit in {"minutes", "hours"} else None
    gaps = []
    if cadence:
        ordered = sorted({parse_ts(c["ts"]) for c in candles})
        for left, right in zip(ordered, ordered[1:]):
            expected = timedelta(minutes=cadence)
            if right - left > expected * 1.5:
                gaps.append({"from": left.isoformat(), "to": right.isoformat(),
                             "missing_estimate": int((right - left) / expected) - 1})
        if gaps:
            findings.append({"check": "gaps", "count": len(gaps),
                             "examples": gaps[:max_gap_reporting]})
            missing += len(gaps)

    ordered_closes = [float(c["close"]) for c in
                      sorted(candles, key=lambda item: parse_ts(item["ts"]))]
    jumps = 0
    for left, right in zip(ordered_closes, ordered_closes[1:]):
        if left > 0 and abs(right / left - 1) > jump_fraction:
            jumps += 1
    if jumps:
        findings.append({"check": "jumps", "count": jumps})
        warning += jumps

    if invalid:
        return QualityReport("INVALID", findings)
    if missing:
        return QualityReport("MISSING", findings)
    if warning:
        return QualityReport("WARNING", findings)
    return QualityReport("VALID", findings)
