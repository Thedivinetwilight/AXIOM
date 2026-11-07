"""Bounded, rate-limit-aware fan-out for per-symbol market-data reads.

Why this exists
---------------
Every market-data provider in this repository is a *per-symbol* callable: one
quote for one instrument key, one completed-bar series for one symbol. No broker
batch endpoint is wired for the scan path, so one watchlist cycle costs two
requests per selected symbol (bars + quote). Driven sequentially, a cycle costs
``n * rtt``; at 100 symbols and a realistic provider round trip that cannot fit
inside the multi-market cycle interval, and it is the sequential fetch loops —
not strategy evaluation — that dominate the cycle.

This module bounds that fan-out:

* a **worker bound** (``max_workers``) so the process never opens an unbounded
  number of provider calls,
* a **request budget** (``max_requests_per_second`` with a burst) so a 100-symbol
  cycle is paced instead of fired at the provider, and an operator can set the
  budget below whatever their entitlement actually allows,
* a **shared cooldown** when a provider answers with a rate-limit error: the
  whole provider backs off (exponentially, capped) instead of every worker
  retrying into the same 429,
* a **whole-batch deadline** so a hung provider call can never stall a cycle,
* **per-symbol isolation**: one exception, timeout or rate limit is reported for
  that symbol only and never changes another symbol's result.

What it deliberately does not do
--------------------------------
It never retries an ambiguous *order* outcome, never touches the execution
boundary (``app.markets.live_execution``), never enlarges a risk limit, and
never hides a failure: every non-OK outcome carries a code the caller records in
its own per-symbol status (``MISSING_DATA`` / ``FAILED`` / ``STALE_DATA``), so
the existing fail-closed behaviour of the callers is preserved.

Determinism: results are returned keyed by symbol in the caller's input order.
The *set* of requests for a cycle is deterministic; only their interleaving on
the wire is not.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable

__all__ = [
    "FetchGateConfig",
    "FetchOutcome",
    "ProviderFetchGate",
    "looks_rate_limited",
]

# Text a provider puts on a throttled answer. Checked case-insensitively after
# the exception type name, so a typed `RateLimitError` and a bare HTTP 429 body
# are both recognised without importing any provider SDK.
RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "request limit exceeded",
    "throttl",
)


def looks_rate_limited(exc: BaseException) -> bool:
    """True when ``exc`` is a provider throttle rather than a data failure."""
    name = type(exc).__name__.lower()
    if "ratelimit" in name or "throttl" in name or "toomanyrequests" in name:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


@dataclass(frozen=True)
class FetchGateConfig:
    """Bounds for one provider's per-symbol fan-out.

    Defaults are a *conservative local* budget, not a claim about any broker's
    published entitlement. An operator whose plan allows less must lower
    ``max_requests_per_second``; an operator with a proven higher entitlement may
    raise it. Nothing here can widen a trading limit.
    """

    max_workers: int = 8
    max_requests_per_second: float = 50.0
    burst: int = 50
    batch_timeout_seconds: float = 45.0
    cooldown_seconds: float = 2.0
    max_cooldown_seconds: float = 30.0
    retries_after_rate_limit: int = 1

    def validate(self) -> "FetchGateConfig":
        object.__setattr__(self, "max_workers", max(1, min(32, int(self.max_workers))))
        object.__setattr__(
            self, "max_requests_per_second", max(0.5, min(200.0, float(self.max_requests_per_second)))
        )
        object.__setattr__(self, "burst", max(1, min(200, int(self.burst))))
        object.__setattr__(
            self, "batch_timeout_seconds", max(1.0, min(300.0, float(self.batch_timeout_seconds)))
        )
        object.__setattr__(self, "cooldown_seconds", max(0.1, min(60.0, float(self.cooldown_seconds))))
        object.__setattr__(
            self, "max_cooldown_seconds",
            max(self.cooldown_seconds, min(300.0, float(self.max_cooldown_seconds))),
        )
        object.__setattr__(
            self, "retries_after_rate_limit", max(0, min(2, int(self.retries_after_rate_limit)))
        )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_workers": self.max_workers,
            "max_requests_per_second": self.max_requests_per_second,
            "burst": self.burst,
            "batch_timeout_seconds": self.batch_timeout_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "max_cooldown_seconds": self.max_cooldown_seconds,
            "retries_after_rate_limit": self.retries_after_rate_limit,
        }


@dataclass
class FetchOutcome:
    """The result of one per-symbol fetch. ``ok`` is the only success signal."""

    symbol: str
    value: Any = None
    error_code: str = ""
    error_detail: str = ""
    attempts: int = 1
    rate_limited: bool = False
    timed_out: bool = False
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error_code


class ProviderFetchGate:
    """Thread-safe limiter + bounded pool shared by every fetch loop of one provider."""

    def __init__(
        self,
        config: FetchGateConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = (config or FetchGateConfig()).validate()
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._tokens = float(self.config.burst)
        # Adaptive budget (AIMD): a provider that answers 429 is telling us the
        # configured budget is above what it will serve. Halve the effective rate
        # and recover it additively on success, instead of failing most symbols.
        self._effective_rps = float(self.config.max_requests_per_second)
        self._success_since_increase = 0
        self._last_refill = self._clock()
        self._cooldown_until = 0.0
        self._cooldown_level = 0
        self._inflight = 0
        self._peak_inflight = 0
        self._stats = {
            "batches": 0,
            "symbols": 0,
            "requests": 0,
            "retries": 0,
            "rate_limited_events": 0,
            "cooldowns": 0,
            "timeouts": 0,
            "errors": 0,
            "wait_seconds": 0.0,
            "workers_used": 0,
            "budget_reductions": 0,
            "budget_increases": 0,
        }

    # -- observability -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._stats)
            out["peak_inflight"] = self._peak_inflight
            out["effective_requests_per_second"] = round(self._effective_rps, 2)
            out["config"] = self.config.to_dict()
            return out

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0.0 if key == "wait_seconds" else 0
            self._peak_inflight = 0

    # -- pacing ------------------------------------------------------------
    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        if elapsed:
            self._tokens = min(
                float(self.config.burst),
                self._tokens + elapsed * float(self._effective_rps),
            )

    def _acquire_slot(self) -> float:
        """Block until this call may go to the provider; returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                if now < self._cooldown_until:
                    delay = self._cooldown_until - now
                else:
                    self._refill_locked(now)
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return waited
                    delay = (1.0 - self._tokens) / float(self._effective_rps)
            delay = min(max(delay, 0.001), 0.1)
            self._sleep(delay)
            waited += delay

    def _note_success(self) -> None:
        with self._lock:
            self._cooldown_level = 0
            self._cooldown_until = 0.0
            self._success_since_increase += 1
            ceiling = float(self.config.max_requests_per_second)
            if self._effective_rps < ceiling and self._success_since_increase >= 25:
                # Additive increase: 10 % of the ceiling per 25 clean successes.
                step = max(1.0, ceiling * 0.1)
                self._effective_rps = min(ceiling, self._effective_rps + step)
                self._success_since_increase = 0
                self._stats["budget_increases"] += 1

    def _note_rate_limited(self) -> None:
        with self._lock:
            now = self._clock()
            self._stats["rate_limited_events"] += 1
            # One cooldown (and one budget cut) per window: N workers hitting the
            # same 429 must not multiply the backoff into minutes.
            if now < self._cooldown_until:
                return
            ceiling = float(self.config.max_requests_per_second)
            # Multiplicative decrease: halve per throttle *window*. Measured
            # against a 20 rps provider: 0.75 left the budget above the entitlement
            # and produced twice the 429s, so the aggressive cut is the safer one.
            reduced = max(1.0, self._effective_rps * 0.5)
            if reduced < self._effective_rps:
                self._effective_rps = reduced
                self._success_since_increase = 0
                self._stats["budget_reductions"] += 1
            self._cooldown_level = min(self._cooldown_level + 1, 6)
            # Exponential backoff, but never longer than 4x the base cooldown:
            # a multi-second stall per throttle is what actually blows a cycle
            # budget, and the level resets on the next success anyway.
            duration = min(
                float(self.config.max_cooldown_seconds),
                float(self.config.cooldown_seconds) * 4.0,
                float(self.config.cooldown_seconds) * (2 ** (self._cooldown_level - 1)),
            )
            self._cooldown_until = now + duration
            self._tokens = 0.0
            self._stats["cooldowns"] += 1

    # -- one symbol --------------------------------------------------------
    @staticmethod
    def _classify(
        exc: BaseException, classify: Callable[[BaseException], tuple[str, str]] | None
    ) -> tuple[str, str]:
        if looks_rate_limited(exc):
            return "RATE_LIMITED", f"provider rate limited the request: {str(exc)[:180]}"
        if classify is not None:
            try:
                code, detail = classify(exc)
                return str(code or "PROVIDER_ERROR"), str(detail or exc)[:200]
            except Exception as classify_exc:  # noqa: BLE001 - a broken classifier must not mask the provider error
                return "PROVIDER_ERROR", (
                    f"{type(exc).__name__}: {str(exc)[:150]} "
                    f"(classifier failed: {type(classify_exc).__name__})"
                )
        return "PROVIDER_ERROR", f"{type(exc).__name__}: {str(exc)[:180]}"

    def _fetch_one(
        self,
        symbol: str,
        fetch: Callable[[str], Any],
        classify: Callable[[BaseException], tuple[str, str]] | None,
    ) -> FetchOutcome:
        attempts = 0
        while True:
            attempts += 1
            waited = self._acquire_slot()
            started = self._clock()
            with self._lock:
                self._stats["requests"] += 1
                if waited:
                    self._stats["wait_seconds"] += waited
                self._inflight += 1
                self._peak_inflight = max(self._peak_inflight, self._inflight)
            try:
                value = fetch(symbol)
            except Exception as exc:  # noqa: BLE001 - one symbol must never break the batch
                code, detail = self._classify(exc, classify)
                rate_limited = code == "RATE_LIMITED"
                if rate_limited:
                    self._note_rate_limited()
                    if attempts <= self.config.retries_after_rate_limit:
                        with self._lock:
                            self._stats["retries"] += 1
                        continue
                with self._lock:
                    self._stats["errors"] += 1
                return FetchOutcome(
                    symbol=symbol,
                    error_code=code,
                    error_detail=detail,
                    attempts=attempts,
                    rate_limited=rate_limited,
                    latency_ms=round((self._clock() - started) * 1000.0, 3),
                )
            finally:
                with self._lock:
                    self._inflight -= 1
            self._note_success()
            return FetchOutcome(
                symbol=symbol,
                value=value,
                attempts=attempts,
                latency_ms=round((self._clock() - started) * 1000.0, 3),
            )

    # -- one cycle ---------------------------------------------------------
    def map(
        self,
        symbols: Iterable[str],
        fetch: Callable[[str], Any],
        *,
        classify: Callable[[BaseException], tuple[str, str]] | None = None,
    ) -> dict[str, FetchOutcome]:
        """Fetch ``symbols`` with bounded concurrency; per-symbol outcomes by key."""
        wanted = [str(s) for s in symbols]
        unique = list(dict.fromkeys(wanted))
        outcomes: dict[str, FetchOutcome] = {}
        if not unique:
            return outcomes

        workers = min(self.config.max_workers, len(unique))
        deadline = self._clock() + self.config.batch_timeout_seconds
        with self._lock:
            self._stats["batches"] += 1
            self._stats["symbols"] += len(unique)
            self._stats["workers_used"] = max(int(self._stats["workers_used"]), workers)

        if workers <= 1:
            for sym in unique:
                if self._clock() >= deadline:
                    outcomes[sym] = FetchOutcome(
                        symbol=sym,
                        error_code="BATCH_DEADLINE_EXCEEDED",
                        error_detail=(
                            f"batch deadline of {self.config.batch_timeout_seconds:.1f}s exceeded before "
                            f"{sym} was attempted"
                        ),
                        timed_out=True,
                    )
                    continue
                outcomes[sym] = self._fetch_one(sym, fetch, classify)
            return outcomes

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="axiom-fetch")
        try:
            futures = {sym: pool.submit(self._fetch_one, sym, fetch, classify) for sym in unique}
            for sym, fut in futures.items():
                remaining = deadline - self._clock()
                try:
                    outcomes[sym] = fut.result(timeout=max(0.05, remaining))
                except TimeoutError:
                    fut.cancel()
                    with self._lock:
                        self._stats["timeouts"] += 1
                    outcomes[sym] = FetchOutcome(
                        symbol=sym,
                        error_code="FETCH_TIMEOUT",
                        error_detail=(
                            f"no provider answer for {sym} within the {self.config.batch_timeout_seconds:.1f}s "
                            "batch deadline; the cycle continues without it"
                        ),
                        timed_out=True,
                    )
        finally:
            # Never join a hung provider thread: the cycle must return.
            pool.shutdown(wait=False, cancel_futures=True)
        return outcomes


def merge_outcomes_into(
    outcomes: dict[str, FetchOutcome],
    *,
    on_ok: Callable[[str, Any], None],
    on_error: Callable[[str, str, str], None],
) -> None:
    """Call the matching callback per outcome, in outcome order (small helper)."""
    for sym, outcome in outcomes.items():
        if outcome.ok:
            on_ok(sym, outcome.value)
        else:
            on_error(sym, outcome.error_code, outcome.error_detail)
