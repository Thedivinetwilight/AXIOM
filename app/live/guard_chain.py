"""Live order guard chain (master spec section 34).

Every live ENTRY passes an ordered chain of independent guards:

    SIGNAL -> PORTFOLIO CHECK -> RISK CHECK -> POSITION CHECK ->
    ORDER VALIDATION -> BROKER RULE CHECK -> DATA FRESHNESS / LATENCY CHECK ->
    DUPLICATE CHECK -> FINAL AUTHORIZATION -> BROKER

Any failed guard means NO NEW ORDER, and the refusal records exactly which
guard refused and why. Two invariants:

* a failed ENTRY guard must never trap an already-held position — risk-
  reducing exits bypass the entry chain (spec: exits remain available
  according to the configured emergency policy);
* a guard that cannot establish its state fails CLOSED for entries.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class GuardResult:
    guard: str
    passed: bool
    detail: str
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {"guard": self.guard, "passed": self.passed,
                "detail": self.detail, "elapsed_ms": round(self.elapsed_ms, 3)}


@dataclass
class ChainVerdict:
    allowed: bool
    is_exit: bool
    results: list[GuardResult] = field(default_factory=list)
    refusing_guard: str | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "is_exit": self.is_exit,
            "refusing_guard": self.refusing_guard,
            "results": [r.to_dict() for r in self.results],
        }


class GuardChain:
    """Ordered, named guards. Each guard is ``fn(order, context) -> (ok, detail)``.

    A guard raising is treated as UNKNOWN state and fails CLOSED (the chain
    records the exception message; it never lets an unverifiable guard pass).
    """

    def __init__(self):
        self._guards: list[tuple[str, Callable[[dict, dict], tuple[bool, str]]]] = []

    def add(self, name: str, fn: Callable[[dict, dict], tuple[bool, str]]) -> "GuardChain":
        self._guards.append((name, fn))
        return self

    @property
    def guard_names(self) -> list[str]:
        return [name for name, _ in self._guards]

    def evaluate(self, order: dict, context: dict) -> ChainVerdict:
        """Run the chain for an order intent.

        ``order`` needs at least ``side`` ("BUY"/"SELL") and ``intent``
        ("ENTRY" | "EXIT" | "RISK_REDUCE"). Exits skip the entry guards after
        ORDER VALIDATION (an exit is still validated and deduplicated), which
        is exactly the "safety guard must never trap a held position" rule.
        """
        is_exit = str(order.get("intent", "")).upper() in ("EXIT", "RISK_REDUCE")
        verdict = ChainVerdict(allowed=True, is_exit=is_exit)
        entry_only = {"SIGNAL", "PORTFOLIO CHECK", "RISK CHECK", "POSITION CHECK",
                      "BROKER RULE CHECK", "DATA FRESHNESS / LATENCY CHECK",
                      "FINAL AUTHORIZATION"}
        for name, fn in self._guards:
            if is_exit and name in entry_only:
                continue
            started = time.perf_counter()
            try:
                ok, detail = fn(order, context)
            except Exception as exc:  # fail closed on unverifiable state
                ok, detail = False, f"guard '{name}' could not verify state: {exc}"
            elapsed = (time.perf_counter() - started) * 1000.0
            result = GuardResult(guard=name, passed=bool(ok), detail=str(detail),
                                 elapsed_ms=elapsed)
            verdict.results.append(result)
            if not result.passed:
                verdict.allowed = False
                verdict.refusing_guard = name
                return verdict
        return verdict


def default_chain(*, settings=None, session_state: str = "OFF",
                  emergency_stop: bool = False,
                  recent_intent_ids: set | None = None,
                  max_entry_age_s: float = 5.0,
                  max_future_skew_s: float = 1.0,
                  clock=time.time) -> GuardChain:
    """The production chain wired to live session state and settings.

    Every closure reads live inputs at evaluation time; nothing is cached at
    construction so a newly-flipped emergency stop is honored immediately.
    """
    chain = GuardChain()

    def signal(order: dict, context: dict):
        if not order.get("strategy"):
            return False, "order carries no strategy identity"
        if not context.get("signal_id"):
            return False, "no signal id attached to the order intent"
        return True, f"signal {context.get('signal_id')} from strategy {order.get('strategy')}"

    def portfolio_check(order: dict, context: dict):
        exposure = context.get("current_exposure", 0.0) or 0.0
        limit = getattr(settings, "max_exposure", None)
        if limit and exposure >= limit:
            return False, f"exposure {exposure} already at limit {limit}"
        return True, f"exposure {exposure} within limit {limit}"

    def risk_check(order: dict, context: dict):
        if context.get("daily_loss_breached"):
            return False, "daily-loss breach in effect"
        if context.get("drawdown_breached"):
            return False, "drawdown breach in effect"
        return True, "no active loss/drawdown breach"

    def position_check(order: dict, context: dict):
        qty = float(order.get("quantity") or 0)
        if qty <= 0:
            return False, "order quantity must be positive"
        max_position = getattr(settings, "max_position_value", None)
        price = float(order.get("price") or 0)
        if max_position and price and qty * price > max_position:
            return False, (f"notional {qty * price:.2f} exceeds max position "
                           f"value {max_position}")
        return True, "position size within configured bounds"

    def order_validation(order: dict, context: dict):
        side = str(order.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            return False, f"unknown side '{order.get('side')}'"
        if not str(order.get("symbol", "")).strip():
            return False, "order has no symbol"
        return True, "order fields structurally valid"

    def broker_rule_check(order: dict, context: dict):
        state = str(context.get("live_state", session_state)).upper()
        if state not in ("ACTIVE",):
            return False, f"live session state is {state}; entries require ACTIVE"
        return True, "live session state permits entries"

    def freshness_check(order: dict, context: dict):
        signal_ts = context.get("signal_ts")
        if signal_ts is None:
            return False, "order has no signal timestamp; cannot verify freshness"
        try:
            timestamp = float(signal_ts)
            now = float(clock())
            max_age = float(max_entry_age_s)
            future_skew = float(max_future_skew_s)
        except (TypeError, ValueError, OverflowError):
            return False, "signal timestamp or freshness bound is malformed"
        if (not math.isfinite(timestamp) or timestamp <= 0
                or not math.isfinite(now) or now <= 0
                or not math.isfinite(max_age) or max_age <= 0
                or not math.isfinite(future_skew) or future_skew < 0):
            return False, "signal timestamp or freshness bound is non-finite/invalid"
        age = now - timestamp
        if age < -future_skew:
            return False, (f"signal timestamp is {-age:.2f}s in the future "
                           f"(max clock skew {future_skew:g}s)")
        if age > max_age:
            return False, f"signal is {age:.1f}s old (max {max_age:g}s)"
        return True, f"signal age {age:.2f}s within bound"

    def duplicate_check(order: dict, context: dict):
        intent_id = context.get("intent_id")
        if not intent_id:
            return False, "order has no deterministic intent id"
        if recent_intent_ids is not None and intent_id in recent_intent_ids:
            return False, f"intent {intent_id} already submitted (duplicate blocked)"
        return True, f"intent {intent_id} not seen before"

    def final_authorization(order: dict, context: dict):
        if emergency_stop or context.get("emergency_stop"):
            return False, "emergency stop is active"
        if context.get("canary_exhausted", False):
            return False, "canary ramp exhausted before completion"
        return True, "final authorization checks clear"

    chain.add("SIGNAL", signal)
    chain.add("PORTFOLIO CHECK", portfolio_check)
    chain.add("RISK CHECK", risk_check)
    chain.add("POSITION CHECK", position_check)
    chain.add("ORDER VALIDATION", order_validation)
    chain.add("BROKER RULE CHECK", broker_rule_check)
    chain.add("DATA FRESHNESS / LATENCY CHECK", freshness_check)
    chain.add("DUPLICATE CHECK", duplicate_check)
    chain.add("FINAL AUTHORIZATION", final_authorization)
    return chain
