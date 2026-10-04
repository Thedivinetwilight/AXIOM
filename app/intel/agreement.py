"""Model agreement / disagreement ledger (master spec §98).

Tracks directional stances between the independent evidence sources named
by the spec:

* ``STATISTICAL_BASELINE`` — measured from recorded bars (momentum, mean
  reversion), never a model output;
* ``STRATEGY`` — the strategy model's own signal;
* ``KRONOS`` — the forecast model (only when real weights are installed —
  a stub must be labelled, see §93);
* ``ANALOGUE`` — the historical-analogue engine's forward-return lean;
* ``REGIME_CONDITIONED`` — a model conditioned on the current regime.

Design rules from the spec:

* disagreement is **not** a failure — it is surfaced explicitly as
  something that may deserve research attention;
* a single source is not a comparison — fewer than two sources is
  ``INSUFFICIENT`` and says so;
* every summary carries the votes it was computed from, so the GUI can
  show *who* disagrees, never a bare score.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from uuid import uuid4

SOURCES = ("STATISTICAL_BASELINE", "STRATEGY", "KRONOS", "ANALOGUE",
           "REGIME_CONDITIONED")

#: stances: +1 bullish, -1 bearish, 0 explicitly no-edge (not "unknown")
STANCES = (-1, 0, 1)

VERDICTS = ("AGREEMENT", "DISAGREEMENT", "MIXED", "NO_EDGE_CONSENSUS",
            "INSUFFICIENT")

logger = logging.getLogger("axiom.intel.agreement")


@dataclass(frozen=True)
class VoteRecord:
    vote_id: str
    ts: float
    market_id: str
    identity: str
    horizon: int
    stances: dict          # source -> stance
    regime: str | None
    provenance: str

    def to_dict(self) -> dict:
        return {"vote_id": self.vote_id, "ts": self.ts,
                "market_id": self.market_id, "identity": self.identity,
                "horizon": self.horizon, "stances": dict(self.stances),
                "regime": self.regime, "provenance": self.provenance}


@dataclass
class AgreementLedger:
    """In-memory, bounded ledger of multi-source directional votes."""

    memory: int = 512
    _records: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._records = deque(maxlen=max(1, int(self.memory)))

    # ------------------------------------------------------------- records --
    def record(self, market_id: str, identity: str, stances: dict, *,
               horizon: int = 5, regime: str | None = None,
               provenance: str = "unspecified") -> VoteRecord:
        if not str(market_id).strip():
            raise ValueError("market_id is required")
        if not str(identity).strip():
            raise ValueError("identity is required")
        if not isinstance(stances, dict) or len(stances) < 2:
            raise ValueError("a comparison needs stances from at least two "
                             "sources; one source is not agreement")
        unknown = sorted(set(stances) - set(SOURCES))
        if unknown:
            raise ValueError(f"unknown source(s): {', '.join(unknown)}")
        for source, stance in stances.items():
            if isinstance(stance, bool) or stance not in STANCES:
                raise ValueError(
                    f"stance for {source} must be one of {STANCES} "
                    f"(got {stance!r}) — use 0 for explicit no-edge")
        if not isinstance(horizon, int) or horizon < 1:
            raise ValueError("horizon must be a positive integer of bars")
        if isinstance(regime, float) and math.isnan(regime):
            raise ValueError("regime label cannot be NaN")
        record = VoteRecord(vote_id=uuid4().hex[:12], ts=time.time(),
                            market_id=str(market_id), identity=str(identity),
                            horizon=int(horizon),
                            stances={s: int(v) for s, v in stances.items()},
                            regime=regime, provenance=str(provenance))
        self._records.append(record)
        return record

    # -------------------------------------------------------------- lookup --
    def votes(self, market_id: str, identity: str,
              horizon: int | None = None) -> list[VoteRecord]:
        out = [r for r in self._records
               if r.market_id == market_id and r.identity == identity]
        if horizon is not None:
            out = [r for r in out if r.horizon == horizon]
        return out

    def latest(self, market_id: str, identity: str,
               horizon: int | None = None) -> VoteRecord | None:
        votes = self.votes(market_id, identity, horizon)
        return votes[-1] if votes else None

    # ------------------------------------------------------------- summary --
    def summary(self, market_id: str, identity: str,
                horizon: int | None = None) -> dict:
        """Latest per-source stance + explicit agreement verdict.

        Disagreement is reported as a research-relevant observation with the
        opposing pairs named — never as an error and never collapsed into a
        single score.
        """
        votes = self.votes(market_id, identity, horizon)
        if not votes:
            return {"market_id": market_id, "identity": identity,
                    "horizon": horizon, "verdict": "INSUFFICIENT",
                    "votes": 0, "stances": {},
                    "note": "no multi-source votes recorded yet"}
        latest = votes[-1]
        stances = dict(latest.stances)
        directional = {s: v for s, v in stances.items() if v != 0}
        if len(directional) < 2:
            verdict = ("NO_EDGE_CONSENSUS" if len(stances) >= 2
                       else "INSUFFICIENT")
            camps: dict = {}
            detail = ("every source is explicitly no-edge"
                      if verdict == "NO_EDGE_CONSENSUS" else
                      "only one directional source; nothing to compare yet")
        else:
            bullish = sorted(s for s, v in directional.items() if v > 0)
            bearish = sorted(s for s, v in directional.items() if v < 0)
            if bullish and bearish:
                verdict = "DISAGREEMENT"
                camps = {"bullish": bullish, "bearish": bearish}
                detail = ("sources disagree on direction — surfaced for "
                          "research attention, not treated as a failure")
            else:
                verdict = "AGREEMENT"
                camps = {"bullish": bullish, "bearish": bearish}
                detail = (f"all directional sources lean "
                          f"{'bullish' if bullish else 'bearish'}")
        return {"market_id": market_id, "identity": identity,
                "horizon": horizon if horizon is not None else latest.horizon,
                "verdict": verdict, "votes": len(votes),
                "stances": stances, "regime": latest.regime,
                "opposed_camps": camps, "vote_id": latest.vote_id,
                "detail": detail,
                "note": "disagreement is surfaced, never treated as failure"}

    def disagreement_watchlist(self) -> list[dict]:
        """Every instrument currently in DISAGREEMENT (research radar, §98)."""
        seen: dict[tuple, dict] = {}
        for record in self._records:
            key = (record.market_id, record.identity)
            seen[key] = self.summary(record.market_id, record.identity)
        return [s for s in seen.values() if s["verdict"] == "DISAGREEMENT"]


# ------------------------------------------------- persistence (§29, §70) --

def save_votes(path, ledger: AgreementLedger) -> None:
    """Durable JSON write (atomic) so votes survive restarts."""
    import json
    import os
    import tempfile

    payload = {"version": 1, "records": [r.to_dict() for r in ledger._records]}
    path = str(path)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as cleanup_error:
            logger.debug("could not remove partial vote file %s: %s",
                         tmp, cleanup_error)
        raise


def load_votes(path, ledger: AgreementLedger) -> int:
    """Restore records from disk; a damaged file yields zero votes, never
    fabricated agreement. Returns the number of records restored."""
    import json
    import os

    if not os.path.exists(str(path)):
        return 0
    try:
        with open(str(path), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return 0
    restored = 0
    for row in payload.get("records", []):
        try:
            record = ledger.record(row["market_id"], row["identity"],
                                   {s: int(v) for s, v in row["stances"].items()},
                                   horizon=int(row.get("horizon", 5)),
                                   regime=row.get("regime"),
                                   provenance=str(row.get("provenance", "restored")))
            restored += 1
        except (KeyError, ValueError, TypeError):
            continue
    return restored
