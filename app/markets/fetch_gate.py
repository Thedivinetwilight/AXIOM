"""Bounded, quota-aware fan-out for per-symbol market-data reads.

Why this exists
---------------
Every market-data provider in this repository is a *per-symbol* callable: one
quote for one instrument key, one completed-bar series for one symbol. No broker
batch endpoint is wired for the scan path, so one watchlist cycle costs two
requests per selected symbol (bars + quote). Driven sequentially, a cycle costs
``n * rtt``; at 100 symbols and a realistic provider round trip that cannot fit
inside the multi-market cycle interval, and it is the sequential fetch loops —
not strategy evaluation — that dominate the cycle.

This module bounds and *budgets* that fan-out:

* a **worker bound** (``max_workers``) so the process never opens an unbounded
  number of provider calls,
* a **request budget** (``max_requests_per_second`` with a burst) so a 100-symbol
  cycle is paced instead of fired at the provider, and an operator can set the
  budget below whatever their entitlement actually allows,
* **documented rolling-window quotas** (:class:`QuotaPolicy`) enforced per
  provider *endpoint category* over every window the provider actually publishes
  (for example Upstox standard APIs: 50/s, 500/min and 2,000/30 min,
  per-API per-user). Requests per second alone is not a provider quota: a burst
  that satisfies 50 rps can still blow a 2,000-per-30-minutes budget,
* a **shared cooldown** when a provider answers with a rate-limit error: the
  whole provider backs off (exponentially, capped) instead of every worker
  retrying into the same 429,
* **AIMD** on the per-second budget: a 429 lowers the effective rate, and it is
  additive-increased back towards the configured ceiling on clean successes.
  A 429 never widens, resets or bypasses a rolling-window budget,
* a **whole-batch deadline** so a hung provider call can never stall a cycle,
* **per-symbol isolation**: one exception, timeout or rate limit is reported for
  that symbol only and never changes another symbol's result.

Quota categories are modelled exactly as the provider documents them
--------------------------------------------------------------------
The policies in :data:`DOCUMENTED_QUOTAS` transcribe the providers' own rate-limit
pages (source URL, read date and the documented wording are carried on each
:class:`QuotaPolicy`). Two rules are followed deliberately:

* **No invented entitlements.** If a plan publishes one number (Alpaca market
  data: 200/min Basic, 10,000/min Algo Trader Plus), that number is used as-is for
  the market-data host. Nothing is scaled, inferred or "rounded up".
* **No invented combinations.** Upstox publishes its limits "on a per-API,
  per-user basis", so the quote endpoint and the candle endpoint each get their
  *own* documented windows rather than one shared pool. Alpaca publishes one
  market-data figure per plan and does not split it per endpoint, so its two
  endpoints (snapshot + bars) share that single documented figure.

The gate enforces limits for **one credential set**. It has no method to rotate
credentials, hosts or accounts, and cycling identities to multiply throughput
would defeat the provider's per-user limit — that is out of scope by design.

What it deliberately does not do
--------------------------------
It never retries an ambiguous *order* outcome, never touches the execution
boundary (``app.markets.live_execution``), never enlarges a risk or trading
limit, and never hides a failure: every non-OK outcome carries a code the caller
records in its own per-symbol status (``MISSING_DATA`` / ``FAILED`` /
``STALE_DATA``), so the existing fail-closed behaviour of the callers is
preserved.

Determinism: results are returned keyed by symbol in the caller's input order.
The *set* of requests for a cycle is deterministic; only their interleaving on
the wire is not.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "DOCUMENTED_QUOTAS",
    "MARKET_ENDPOINT_CATEGORIES",
    "FetchGateConfig",
    "FetchOutcome",
    "ProviderFetchGate",
    "QuotaDeferred",
    "QuotaPolicy",
    "QuotaWindow",
    "alpaca_market_data_policies",
    "looks_rate_limited",
    "upstox_standard_policies",
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


# --------------------------------------------------------------------------- #
# Documented provider quotas
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuotaWindow:
    """One published quota window: at most ``limit`` requests per ``seconds``."""

    name: str
    seconds: float
    limit: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "seconds": self.seconds, "limit": self.limit}


@dataclass(frozen=True)
class QuotaPolicy:
    """The documented windows for one provider endpoint *category*.

    ``category`` is the key the gate is asked to admit a request under; one
    category equals one quota pool. ``documented_as`` keeps the provider's own
    wording next to the numbers so the mapping can be audited without a browser.
    """

    category: str
    label: str
    windows: tuple[QuotaWindow, ...]
    source_url: str
    documented_as: str
    checked_on: str = "2026-11-07"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "label": self.label,
            "windows": [w.to_dict() for w in self.windows],
            "source_url": self.source_url,
            "documented_as": self.documented_as,
            "checked_on": self.checked_on,
        }

    @property
    def tightest_requests_per_second(self) -> float:
        """The lowest sustained rate (req/s) any single documented window allows."""
        rates = [w.limit / w.seconds for w in self.windows if w.seconds > 0]
        return min(rates) if rates else float("inf")


# Upstox, "Other Standard APIs (holdings, positions, funds, historical candles
# etc.)": 50/second, 500/minute, 2000/30 minutes, enforced "on a per-API,
# per-user basis".
_UPSTOX_STANDARD_WINDOWS = (
    QuotaWindow("1s", 1.0, 50),
    QuotaWindow("1m", 60.0, 500),
    QuotaWindow("30m", 1800.0, 2000),
)
_UPSTOX_SOURCE = "https://upstox.com/developer/api-documentation/rate-limiting/#other-standard-apis"
_UPSTOX_WORDING = (
    "Other Standard APIs (holdings, positions, funds, historical candles etc.): "
    "per second 50 requests, per minute 500 requests, per 30 minutes 2000 requests; "
    "rate limits are enforced on a per-API, per-user basis"
)

# Alpaca, market data: one figure per plan for the market-data host, measured
# per owner. The documentation does not publish per-second or 30-minute windows
# for this product, so none are invented here.
_ALPACA_SOURCE = "https://docs.alpaca.markets/us/docs/about-market-data-api#subscription-plans"
_ALPACA_PLAN_RPM = {
    "basic": 200,
    "algo_trader_plus": 10000,
}


def upstox_standard_policies() -> tuple[QuotaPolicy, ...]:
    """Per-endpoint Upstox policies for the two endpoints AXIOM calls.

    The quote endpoint and the candle endpoint are separate APIs, so each is a
    separate quota pool — exactly as the provider says ("per-API, per-user").
    Intraday and historical candles are the same *documented category*
    ("historical candles"), so both candle paths draw on the one candle pool.
    """
    return (
        QuotaPolicy(
            category="upstox.market_quote",
            label="Upstox GET /v2/market-quote/quotes",
            windows=_UPSTOX_STANDARD_WINDOWS,
            source_url=_UPSTOX_SOURCE,
            documented_as=_UPSTOX_WORDING,
        ),
        QuotaPolicy(
            category="upstox.historical_candle",
            label="Upstox GET /v3/historical-candle (and /intraday)",
            windows=_UPSTOX_STANDARD_WINDOWS,
            source_url=_UPSTOX_SOURCE,
            documented_as=_UPSTOX_WORDING,
        ),
    )


def alpaca_market_data_policies(plan: str = "basic") -> tuple[QuotaPolicy, ...]:
    """Alpaca market-data policy for the documented plan.

    One category covers both endpoints AXIOM calls (``/v2/stocks/bars`` and
    ``/v2/stocks/{symbol}/snapshot``) because the provider publishes a single
    market-data figure per plan rather than a per-endpoint one.
    """
    key = str(plan or "basic").strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _ALPACA_PLAN_RPM:
        raise ValueError(
            f"unknown Alpaca market-data plan {plan!r}; documented plans are "
            f"{sorted(_ALPACA_PLAN_RPM)}"
        )
    rpm = _ALPACA_PLAN_RPM[key]
    return (
        QuotaPolicy(
            category="alpaca.market_data",
            label=f"Alpaca market data ({key}: {rpm}/min)",
            windows=(QuotaWindow("1m", 60.0, rpm),),
            source_url=_ALPACA_SOURCE,
            documented_as=(
                f"Market Data API subscription plan {key}: historical/market-data API calls "
                f"{rpm}/min (per owner). No per-second or 30-minute window is documented for "
                "this product."
            ),
        ),
    )


DOCUMENTED_QUOTAS: Mapping[str, tuple[QuotaPolicy, ...]] = {
    "upstox_standard": upstox_standard_policies(),
    "alpaca_basic": alpaca_market_data_policies("basic"),
    "alpaca_algo_trader_plus": alpaca_market_data_policies("algo_trader_plus"),
}

# Which quota category each market's quote / bar call belongs to. Kept here so
# the engine, the ensemble service, the API wiring and the tests cannot drift.
MARKET_ENDPOINT_CATEGORIES: Mapping[str, Mapping[str, str]] = {
    "NSE": {"quote": "upstox.market_quote", "bars": "upstox.historical_candle"},
    "NASDAQ": {"quote": "alpaca.market_data", "bars": "alpaca.market_data"},
}


@dataclass(frozen=True)
class FetchGateConfig:
    """Bounds for one provider's per-symbol fan-out.

    Defaults are a *conservative local* budget, not a claim about any broker's
    published entitlement. An operator whose plan allows less must lower
    ``max_requests_per_second``; an operator with a proven higher entitlement may
    raise it. Nothing here can widen a trading limit.

    ``quota_policies`` carries the provider's documented windows. With no
    policies the gate still paces by ``max_requests_per_second`` (previous
    behaviour); with policies every request is additionally admitted against
    each documented window of its endpoint category.
    """

    max_workers: int = 8
    max_requests_per_second: float = 50.0
    burst: int = 50
    batch_timeout_seconds: float = 45.0
    cooldown_seconds: float = 2.0
    max_cooldown_seconds: float = 30.0
    retries_after_rate_limit: int = 1
    quota_policies: tuple[QuotaPolicy, ...] = ()
    quota_profile: str = "custom"

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
        policies: list[QuotaPolicy] = []
        seen: set[str] = set()
        for policy in tuple(self.quota_policies or ()):
            if policy.category in seen:
                raise ValueError(f"duplicate quota policy for category {policy.category!r}")
            seen.add(policy.category)
            windows = tuple(
                QuotaWindow(str(w.name), max(0.1, float(w.seconds)), max(1, int(w.limit)))
                for w in policy.windows
            )
            policies.append(
                QuotaPolicy(
                    category=policy.category,
                    label=policy.label,
                    windows=windows,
                    source_url=policy.source_url,
                    documented_as=policy.documented_as,
                    checked_on=policy.checked_on,
                )
            )
        object.__setattr__(self, "quota_policies", tuple(policies))
        return self

    def policy_for(self, category: str) -> QuotaPolicy | None:
        for policy in self.quota_policies:
            if policy.category == category:
                return policy
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_workers": self.max_workers,
            "max_requests_per_second": self.max_requests_per_second,
            "burst": self.burst,
            "batch_timeout_seconds": self.batch_timeout_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "max_cooldown_seconds": self.max_cooldown_seconds,
            "retries_after_rate_limit": self.retries_after_rate_limit,
            "quota_profile": self.quota_profile,
            "quota_policies": [p.to_dict() for p in self.quota_policies],
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


class QuotaDeferred(RuntimeError):
    """A request could not be admitted before the batch deadline.

    Raised instead of sleeping past a cycle's deadline: the request is **not**
    sent, so it consumes no provider quota and cannot land after the cycle that
    asked for it has already been abandoned. Callers report it per symbol.
    """

    def __init__(self, category: str | None, wait_seconds: float) -> None:
        self.category = category
        self.wait_seconds = round(float(wait_seconds), 3)
        super().__init__(
            f"quota deferred for {category or 'provider'}: the next admission needs "
            f"{self.wait_seconds:.1f}s of documented quota, past the batch deadline"
        )


class ProviderFetchGate:
    """Thread-safe limiter + bounded pool shared by every fetch loop of one provider.

    One gate is one credential set for one provider (market). Every request it
    admits is charged to the endpoint category the caller names, against every
    documented window of that category, plus the per-second budget.
    """

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
        # This only ever affects the per-second budget; the documented rolling
        # windows below are never widened, reset or skipped by a 429.
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
            "quota_waits": 0,
            "quota_wait_seconds": 0.0,
            "deferred": 0,
        }
        # category -> {window name -> deque of admission timestamps}
        # Deadline of the batch each worker thread is serving, so an admission
        # that would sleep past the cycle's deadline defers instead of issuing a
        # request nobody is waiting for any more.
        self._thread_deadlines: dict[int, float] = {}
        self._quota_windows: dict[str, dict[str, deque[float]]] = {}
        self._quota_stats: dict[str, dict[str, Any]] = {}
        for policy in self.config.quota_policies:
            self._quota_windows[policy.category] = {w.name: deque() for w in policy.windows}
            self._quota_stats[policy.category] = {
                "requests": 0,
                "rate_limited_events": 0,
                "quota_waits": 0,
                "quota_wait_seconds": 0.0,
                "window_blocks": {w.name: 0 for w in policy.windows},
                "window_peak": {w.name: 0 for w in policy.windows},
            }

    # -- observability -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._stats)
            out["wait_seconds"] = round(float(out["wait_seconds"]), 3)
            out["quota_wait_seconds"] = round(float(out["quota_wait_seconds"]), 3)
            out["peak_inflight"] = self._peak_inflight
            out["effective_requests_per_second"] = round(self._effective_rps, 2)
            out["config"] = self.config.to_dict()
            out["quota"] = self.quota_report()
            return out

    def quota_report(self) -> dict[str, Any]:
        """Per-category usage against the documented windows (for reports/audit)."""
        with self._lock:
            now = self._clock()
            report: dict[str, Any] = {"categories": {}, "policies": {}}
            for policy in self.config.quota_policies:
                stat = self._quota_stats[policy.category]
                windows = self._quota_windows[policy.category]
                per_window: dict[str, Any] = {}
                for window in policy.windows:
                    dq = windows[window.name]
                    self._evict_locked(dq, now, window.seconds)
                    per_window[window.name] = {
                        "seconds": window.seconds,
                        "limit": window.limit,
                        "current": len(dq),
                        "peak": stat["window_peak"][window.name],
                        "blocks": stat["window_blocks"][window.name],
                    }
                report["categories"][policy.category] = {
                    "label": policy.label,
                    "requests": stat["requests"],
                    "rate_limited_events": stat["rate_limited_events"],
                    "quota_waits": stat["quota_waits"],
                    "quota_wait_seconds": round(float(stat["quota_wait_seconds"]), 3),
                    "windows": per_window,
                    "tightest_requests_per_second": round(policy.tightest_requests_per_second, 3),
                }
                report["policies"][policy.category] = policy.to_dict()
            return report

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0.0 if "seconds" in key else 0
            self._peak_inflight = 0
            for stat in self._quota_stats.values():
                stat["requests"] = 0
                stat["rate_limited_events"] = 0
                stat["quota_waits"] = 0
                stat["quota_wait_seconds"] = 0.0
                for name in stat["window_blocks"]:
                    stat["window_blocks"][name] = 0
                    stat["window_peak"][name] = 0

    # -- window bookkeeping ------------------------------------------------
    @staticmethod
    def _evict_locked(dq: deque[float], now: float, seconds: float) -> None:
        horizon = now - seconds
        while dq and dq[0] <= horizon:
            dq.popleft()

    def _window_delay_locked(self, category: str | None, now: float) -> float:
        """Seconds until ``category`` has room in *every* documented window."""
        if not category:
            return 0.0
        policy = self.config.policy_for(category)
        if policy is None:
            return 0.0
        windows = self._quota_windows[category]
        delay = 0.0
        for window in policy.windows:
            dq = windows[window.name]
            self._evict_locked(dq, now, window.seconds)
            if len(dq) >= window.limit:
                wait = (dq[0] + window.seconds) - now
                delay = max(delay, wait + 0.001)
        return delay

    def _blocked_windows_locked(self, category: str | None, now: float) -> list[str]:
        """Names of the documented windows currently at their limit."""
        if not category:
            return []
        policy = self.config.policy_for(category)
        if policy is None:
            return []
        windows = self._quota_windows[category]
        blocked: list[str] = []
        for window in policy.windows:
            dq = windows[window.name]
            self._evict_locked(dq, now, window.seconds)
            if len(dq) >= window.limit:
                blocked.append(window.name)
        return blocked

    def _record_window_locked(self, category: str | None, now: float) -> None:
        if not category:
            return
        policy = self.config.policy_for(category)
        if policy is None:
            return
        windows = self._quota_windows[category]
        stat = self._quota_stats[category]
        for window in policy.windows:
            dq = windows[window.name]
            self._evict_locked(dq, now, window.seconds)
            dq.append(now)
            if len(dq) > stat["window_peak"][window.name]:
                stat["window_peak"][window.name] = len(dq)
        stat["requests"] += 1

    def quota_usage(self, category: str) -> dict[str, Any]:
        """Current admission counts per documented window for ``category``."""
        with self._lock:
            policy = self.config.policy_for(category)
            if policy is None:
                return {}
            now = self._clock()
            out = {}
            for window in policy.windows:
                dq = self._quota_windows[category][window.name]
                self._evict_locked(dq, now, window.seconds)
                out[window.name] = len(dq)
            return out

    # -- pacing ------------------------------------------------------------
    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        if elapsed:
            self._tokens = min(
                float(self.config.burst),
                self._tokens + elapsed * float(self._effective_rps),
            )

    def admit(self, category: str | None = None, *, deadline: float | None = None) -> float:
        """Block until one request in ``category`` may go out; returns seconds waited.

        Admission is granted only when *all* of these allow it: no provider
        cooldown is active, the per-second token bucket has a token, and every
        documented rolling window of ``category`` has room. A 429 lowers the
        token-bucket rate only — it never clears a window.
        """
        waited = 0.0
        blocked: set[str] = set()
        while True:
            with self._lock:
                now = self._clock()
                if now < self._cooldown_until:
                    delay = self._cooldown_until - now
                else:
                    self._refill_locked(now)
                    window_delay = self._window_delay_locked(category, now)
                    token_delay = 0.0 if self._tokens >= 1.0 else (1.0 - self._tokens) / float(
                        self._effective_rps
                    )
                    if window_delay <= 0.0 and token_delay <= 0.0:
                        self._tokens -= 1.0
                        self._record_window_locked(category, now)
                        if waited:
                            self._stats["quota_waits"] += 1
                            self._stats["quota_wait_seconds"] += waited
                            stat = self._quota_stats.get(category or "")
                            if stat is not None:
                                stat["quota_waits"] += 1
                                stat["quota_wait_seconds"] += waited
                                for name in blocked:
                                    stat["window_blocks"][name] += 1
                        return waited
                    if window_delay > 0.0:
                        blocked.update(self._blocked_windows_locked(category, now))
                    delay = max(window_delay, token_delay)
                hard_deadline = deadline if deadline is not None else self._thread_deadlines.get(
                    threading.get_ident()
                )
                if hard_deadline is not None and now + delay > hard_deadline:
                    raise QuotaDeferred(category, delay)
            delay = min(max(delay, 0.001), 0.1)
            self._sleep(delay)
            waited += delay

    def _acquire_slot(self, category: str | None = None) -> float:
        """Back-compat alias: admit one request in ``category`` and account the wait."""
        waited = self.admit(category)
        if waited:
            with self._lock:
                self._stats["wait_seconds"] += waited
        return waited

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

    def _note_rate_limited(self, category: str | None = None) -> None:
        with self._lock:
            now = self._clock()
            self._stats["rate_limited_events"] += 1
            stat = self._quota_stats.get(category or "")
            if stat is not None:
                stat["rate_limited_events"] += 1
            # One cooldown (and one budget cut) per window: N workers hitting the
            # same 429 must not multiply the backoff into minutes.
            if now < self._cooldown_until:
                return
            # Multiplicative decrease on the per-second budget only. The rolling
            # window deques are deliberately untouched: a throttle must never
            # hand back 30-minute quota that the provider already counted.
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

    def guarded(
        self, category: str | None, call: Callable[..., Any]
    ) -> Callable[..., Any]:
        """Wrap a provider callable so every call is admitted under ``category``.

        This is the enforcement point for providers that the engine may call
        directly (outside a batch): the request is admitted against the
        documented windows, a 429 backs the whole provider off and is retried at
        most ``retries_after_rate_limit`` times, and any other exception is
        re-raised unchanged so callers keep their own classification.
        """

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            attempts = 0
            while True:
                attempts += 1
                self._acquire_slot(category)
                with self._lock:
                    self._stats["requests"] += 1
                try:
                    value = call(*args, **kwargs)
                except QuotaDeferred:
                    raise
                except Exception as exc:  # noqa: BLE001 - re-raised after the retry budget
                    if looks_rate_limited(exc):
                        self._note_rate_limited(category)
                        if attempts <= self.config.retries_after_rate_limit:
                            with self._lock:
                                self._stats["retries"] += 1
                            continue
                    raise
                self._note_success()
                return value

        _wrapped.__name__ = getattr(call, "__name__", "guarded_provider")
        _wrapped.__doc__ = getattr(call, "__doc__", None)
        _wrapped.__wrapped__ = call  # type: ignore[attr-defined]
        _wrapped.fetch_gate_category = category  # type: ignore[attr-defined]
        return _wrapped

    def _fetch_one(
        self,
        symbol: str,
        fetch: Callable[[str], Any],
        classify: Callable[[BaseException], tuple[str, str]] | None,
        category: str | None = None,
    ) -> FetchOutcome:
        attempts = 0
        while True:
            attempts += 1
            self._acquire_slot(category)
            started = self._clock()
            with self._lock:
                self._stats["requests"] += 1
                self._inflight += 1
                self._peak_inflight = max(self._peak_inflight, self._inflight)
            try:
                value = fetch(symbol)
            except QuotaDeferred as deferred:
                with self._lock:
                    self._stats["deferred"] += 1
                return FetchOutcome(
                    symbol=symbol,
                    error_code="QUOTA_DEFERRED",
                    error_detail=str(deferred),
                    attempts=attempts,
                    latency_ms=round((self._clock() - started) * 1000.0, 3),
                )
            except Exception as exc:  # noqa: BLE001 - one symbol must never break the batch
                code, detail = self._classify(exc, classify)
                rate_limited = code == "RATE_LIMITED"
                if rate_limited:
                    self._note_rate_limited(category)
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
        category: str | None = None,
        paced: bool = True,
    ) -> dict[str, FetchOutcome]:
        """Fetch ``symbols`` with bounded concurrency; per-symbol outcomes by key.

        ``paced=True`` (default) admits one request per symbol in ``category``
        before calling ``fetch`` — correct when ``fetch`` issues a single
        provider request. Pass ``paced=False`` when ``fetch`` already admits its
        own requests (for example a caller whose providers were wrapped with
        :meth:`guarded` and which issues two endpoints per symbol), so a request
        is never charged to a quota twice.
        """
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

        def _run(func, sym, *args):
            """Run a symbol fetch with this thread's batch deadline registered."""
            self._thread_deadlines[threading.get_ident()] = deadline
            try:
                return func(sym, *args)
            finally:
                self._thread_deadlines.pop(threading.get_ident(), None)

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
                if paced:
                    outcomes[sym] = _run(self._fetch_one, sym, fetch, classify, category)
                else:
                    outcomes[sym] = _run(self._fetch_unpaced, sym, fetch, classify)
            return outcomes

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="axiom-fetch")
        try:
            if paced:
                futures = {
                    sym: pool.submit(_run, self._fetch_one, sym, fetch, classify, category)
                    for sym in unique
                }
            else:
                futures = {
                    sym: pool.submit(_run, self._fetch_unpaced, sym, fetch, classify)
                    for sym in unique
                }
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

    def _fetch_unpaced(
        self,
        symbol: str,
        fetch: Callable[[str], Any],
        classify: Callable[[BaseException], tuple[str, str]] | None,
    ) -> FetchOutcome:
        """Call ``fetch`` without admission: the callable admits its own requests.

        Request accounting also belongs to the callable here, so a symbol that
        issues two endpoint requests is charged twice and never once.
        """
        started = self._clock()
        with self._lock:
            self._inflight += 1
            self._peak_inflight = max(self._peak_inflight, self._inflight)
        try:
            value = fetch(symbol)
        except QuotaDeferred as deferred:
            with self._lock:
                self._stats["deferred"] += 1
            return FetchOutcome(
                symbol=symbol,
                error_code="QUOTA_DEFERRED",
                error_detail=str(deferred),
                attempts=1,
                latency_ms=round((self._clock() - started) * 1000.0, 3),
            )
        except Exception as exc:  # noqa: BLE001 - one symbol must never break the batch
            code, detail = self._classify(exc, classify)
            with self._lock:
                self._stats["errors"] += 1
            return FetchOutcome(
                symbol=symbol,
                error_code=code,
                error_detail=detail,
                attempts=1,
                rate_limited=code == "RATE_LIMITED",
                latency_ms=round((self._clock() - started) * 1000.0, 3),
            )
        finally:
            with self._lock:
                self._inflight -= 1
        return FetchOutcome(
            symbol=symbol,
            value=value,
            attempts=1,
            latency_ms=round((self._clock() - started) * 1000.0, 3),
        )


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
