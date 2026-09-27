"""Event-driven decision triggers + safe component pause.

Triggers (each one documented, deterministic):
  - cadence (the ONLY unconditional trigger: strategy's own candle rhythm)
  - significant move (|close/prev − 1| ≥ move_threshold)
  - volume spike (candle volume ≥ volume_spike × mean of the previous 5)
  - regime change (regime engine reports a different regime)
  - order events (broker/order lifecycle)
Time passing alone NEVER triggers a decision. PauseGate persists safe
component pauses in engine state so they survive restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Decision:
    trigger: str
    strategy: str
    detail: dict = field(default_factory=dict)


@dataclass
class TriggerState:
    last_candle_index: int = 0
    last_price: float | None = None
    last_regime: str | None = None
    last_volatility_shift: bool | None = None  # None = no baseline yet


class EventDrivenDecisionEngine:
    def __init__(self, *, move_threshold: float = 0.003,
                 volume_spike: float = 2.0):
        self.move_threshold = float(move_threshold)
        self.volume_spike = float(volume_spike)
        self._cadence: dict[str, int] = {}
        self._state = TriggerState()

    def register_cadence(self, strategy: str, every_n: int) -> None:
        if every_n < 1:
            raise ValueError("cadence must be >= 1 candle")
        self._cadence[strategy] = int(every_n)

    def cadence_for(self, strategy: str) -> int | None:
        """Registered cadence for a strategy (None = none registered)."""
        return self._cadence.get(strategy)

    def on_candle(self, candle, history: list, regime_engine=None) -> list[Decision]:
        decisions: list[Decision] = []
        self._state.last_candle_index += 1
        index = self._state.last_candle_index
        # 1. cadence trigger (the ONLY unconditional one)
        for strategy, every_n in self._cadence.items():
            if index % every_n == 0:
                decisions.append(Decision("cadence", strategy,
                                          {"candle_index": index}))
        # 2. significant move
        if self._state.last_price:
            change = candle.close / self._state.last_price - 1
            if abs(change) >= self.move_threshold:
                decisions.append(Decision(
                    "significant_move", "*",
                    {"change": round(change, 6),
                     "from": self._state.last_price, "to": candle.close}))
        self._state.last_price = candle.close
        # 3. volume spike vs recent average
        if len(history) >= 6:
            recent = [c.volume for c in history[-6:-1]]
            average = sum(recent) / len(recent)
            if average > 0 and candle.volume >= average * self.volume_spike:
                decisions.append(Decision("volume_spike", "*",
                                          {"volume": candle.volume,
                                           "recent_avg": average}))
        # 4. volatility change (documented: stdev of last 5 close-to-close
        # returns vs the previous 5; ≥2× shift fires once per shift)
        if len(history) >= 10:
            def _vol(candles):
                rets = [candles[i].close / candles[i - 1].close - 1
                        for i in range(1, len(candles))]
                mean = sum(rets) / len(rets)
                return (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
            recent_vol = _vol(history[-5:])
            previous_vol = _vol(history[-10:-5])
            if previous_vol > 0:
                shift = recent_vol / previous_vol
                if (shift >= 2.0 or shift <= 0.5) and \
                        self._state.last_volatility_shift != (shift >= 2.0):
                    decisions.append(Decision(
                        "volatility_change", "*",
                        {"recent_vol": round(recent_vol, 8),
                         "previous_vol": round(previous_vol, 8),
                         "shift": round(shift, 4)}))
                self._state.last_volatility_shift = shift >= 2.0
        # 5. regime change
        if regime_engine is not None:
            regime = regime_engine.current
            if regime and self._state.last_regime and regime != self._state.last_regime:
                decisions.append(Decision("regime_change", "*",
                                          {"from": self._state.last_regime,
                                           "to": regime}))
            if regime:
                self._state.last_regime = regime
        return decisions

    def on_order_event(self, event: dict) -> list[Decision]:
        return [Decision("order_event", str(event.get("strategy", "*")), event)]


class PauseGate:
    """Safe component pause backed by persisted engine state."""

    def __init__(self, repository):
        self.repository = repository

    def is_paused(self, component: str) -> bool:
        flag = self.repository.get_engine_state(f"paused:{component}", {}) or {}
        return bool(flag.get("paused"))

    def pause(self, component: str, reason: str) -> None:
        self.repository.set_engine_state(f"paused:{component}",
                                         {"paused": True, "reason": reason})
        self.repository.record_system_event("COMPONENT_PAUSED",
                                            {"component": component,
                                             "reason": reason})

    def resume(self, component: str) -> None:
        self.repository.set_engine_state(f"paused:{component}", {"paused": False})
        self.repository.record_system_event("COMPONENT_RESUMED",
                                            {"component": component})
