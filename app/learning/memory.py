"""MarketMemory: historical regime fingerprints + similarity lookup.

Stores observable feature snapshots per regime observation and retrieves the
k most similar past windows (Euclidean distance on normalized features).
SIMILARITY IS OBSERVATION, NOT PREDICTION: results carry an explicit
disclaimer — past regime similarity guarantees nothing about the future.
"""

from __future__ import annotations

import json
import math

FEATURE_KEYS = ("trend", "volatility", "momentum", "volume_trend")

DISCLAIMER = ("Similarity to past regimes is an observation about recorded data; "
              "it is NOT a guarantee of future behavior.")


def _vector(features: dict) -> list[float]:
    return [float(features.get(key) or 0.0) for key in FEATURE_KEYS]


class MarketMemory:
    def __init__(self, repository):
        self.repository = repository

    def record(self, ts: str, regime: str, features: dict,
               instrument: str = "MARKET") -> None:
        self.repository.record_regime_observation(ts, instrument, regime, features)

    def similar(self, features: dict, *, k: int = 5,
                instrument: str = "MARKET") -> dict:
        target = _vector(features)
        rows = self.repository.list_regime_history(instrument)
        scored = []
        for row in rows:
            payload = row["features"]
            payload = json.loads(payload) if isinstance(payload, str) else payload
            vector = _vector(payload)
            distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(target, vector)))
            scored.append((distance, row["ts"], row["regime"], payload))
        scored.sort(key=lambda item: item[0])
        return {
            "matches": [{"ts": ts, "regime": regime,
                         "distance": round(distance, 6), "features": payload}
                        for distance, ts, regime, payload in scored[:k]],
            "disclaimer": DISCLAIMER,
        }

    @staticmethod
    def similarity_disclaimer() -> str:
        return DISCLAIMER
