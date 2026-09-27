"""Learning journal (V1): persist signals/trades/regimes for offline study.

Records are APPEND-ONLY research data. The journal cannot execute anything,
cannot change risk limits, and cannot enable trading modes — it only writes
rows through the existing Repository.
"""

from datetime import datetime, timezone
from uuid import uuid4

from app.learning.regime import classify_regime


class LearningJournal:
    def __init__(self, repository, params_version: str = "baseline"):
        self.repository = repository
        self.params_version = params_version

    def record_signal(self, *, strategy: str, symbol: str, timeframe: str,
                      signal: str, price: float, candles: list, meta: dict | None = None) -> str:
        regime = classify_regime(candles)
        record = {
            "id": uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "params_version": self.params_version,
            "symbol": symbol,
            "timeframe": timeframe,
            "regime": regime,
            "signal": signal,
            "price": float(price),
            "meta": meta or {},
        }
        self.repository.record_signal(record)
        return regime

    def record_outcome(self, *, strategy: str, symbol: str, entry: float, exit: float,
                       quantity: float, pnl: float, fees: float, slippage: float,
                       duration_bars: int, outcome: str, regime: str,
                       params_version: str | None = None) -> str:
        """Persist one closed-trade observation for rolling-batch evaluation."""
        payload = {
            "id": uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "trade_outcome",
            "strategy": strategy,
            "params_version": params_version or self.params_version,
            "symbol": symbol,
            "entry": float(entry),
            "exit": float(exit),
            "quantity": float(quantity),
            "pnl": float(pnl),
            "fees": float(fees),
            "slippage": float(slippage),
            "duration_bars": int(duration_bars),
            "outcome": outcome,  # win | loss | flat
            "regime": regime,
        }
        self.repository.record_system_event("TRADE_OUTCOME", payload)
        return payload["id"]
