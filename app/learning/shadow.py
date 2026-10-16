"""ShadowStrategyEngine: candidates trade HYPOTHETICALLY — never real orders.

Shadow candidates consume the same completed candles as live strategies and
their hypothetical BUY/SELL/HOLD decisions + paper outcomes are recorded in
`shadow_trades`. No broker, no risk-gate bypass, no orders: integration only
ever passes candles, and tests assert the broker sees nothing.
"""

from __future__ import annotations

import json



def candidate_id(strategy: str, parameters: dict) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps({"strategy": strategy, "parameters": parameters}, sort_keys=True).encode()
    ).hexdigest()[:16]


class ShadowCandidate:
    def __init__(self, strategy, parameters: dict, unit: str, interval: int):
        self.strategy = strategy
        self.parameters = dict(parameters)
        self.id = candidate_id(strategy.name, self.parameters)
        self.position: dict | None = None
        self.trades = 0
        self.realized = 0.0
        self.history: list = []

    def on_candle(self, candle, repository, regime: str | None) -> str:
        from app.strategies.base import Signal

        limit = max(getattr(self.strategy, "history_limit", 30), 5)
        self.history.append(candle)
        self.history[:] = self.history[-limit:]
        signal = self.strategy.generate_signal(tuple(self.history))
        action = signal.value
        result: dict = {"position_before": bool(self.position)}
        if signal in {Signal.BUY} and self.position is None:
            self.position = {"entry": candle.close, "ts": candle.timestamp.isoformat()}
        elif signal in {Signal.SELL, Signal.EXIT} and self.position is not None:
            pnl = candle.close - self.position["entry"]
            self.realized += pnl
            self.trades += 1
            result.update({"closed": True, "pnl": pnl,
                           "entry": self.position["entry"], "exit": candle.close,
                           "duration_bars": self.trades})
            self.position = None
        result.update({"hypothetical_position": self.position, "cumulative_pnl": self.realized})
        if repository is not None:
            repository.record_shadow_trade(
                strategy=self.strategy.name, parameters=self.parameters,
                instrument_key=candle.symbol, action=action, price=candle.close,
                regime=regime, unit=self.unit_name, interval=self.interval_value,
                result=result, ts=candle.timestamp.isoformat())
        return action

    unit_name = "minutes"
    interval_value = 1


class ShadowStrategyEngine:
    def __init__(self, repository, regime_engine: RegimeEngineLike | None = None):
        self.repository = repository
        self.regime_engine = regime_engine
        self.candidates: dict[str, ShadowCandidate] = {}

    def register(self, strategy, parameters: dict, unit: str = "minutes",
                 interval: int = 1) -> str:
        candidate = ShadowCandidate(strategy, parameters, unit, interval)
        candidate.unit_name = unit
        candidate.interval_value = int(interval)
        self.candidates[candidate.id] = candidate
        return candidate.id

    def on_candle(self, candle) -> dict[str, str]:
        regime = self.regime_engine.current if self.regime_engine else None
        return {cid: candidate.on_candle(candle, self.repository, regime)
                for cid, candidate in self.candidates.items()}

    def performance(self) -> dict:
        return {cid: {"trades": candidate.trades, "realized": candidate.realized,
                      "in_position": candidate.position is not None,
                      "strategy": candidate.strategy.name,
                      "parameters": candidate.parameters}
                for cid, candidate in self.candidates.items()}


class RegimeEngineLike:
    current: str | None = None
