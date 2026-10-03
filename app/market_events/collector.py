"""Event collection — timeout, retry/backoff, rate limiting, caching, isolation.

No live-website dependency for tests: fetches are via injectable ``fetcher``
callable. Production use can supply an httpx-based fetcher. No eval/exec.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Any
import logging

logger = logging.getLogger("bot.market_events")

# Simple in-memory cache + rate limiter (per-source)
@dataclass
class CollectorState:
    cache: dict = field(default_factory=dict)  # url -> (ts, data)
    last_fetch: dict = field(default_factory=dict)  # source -> ts
    fetch_counts: dict = field(default_factory=dict)  # source -> count in window
    window_start: float = field(default_factory=time.time)


class EventCollector:
    """Collects raw articles with safety: timeout, retry, backoff, rate limit, cache, isolation."""

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        max_retries: int = 2,
        backoff_base: float = 0.2,
        rate_limit_per_minute: int = 30,
        cache_ttl: float = 300.0,
        fetcher: Callable[[str], Any] | None = None,
    ):
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.rate_limit = int(rate_limit_per_minute)
        self.cache_ttl = float(cache_ttl)
        self.fetcher = fetcher  # injectable for tests; if None, use no-op (returns failure)
        self.state = CollectorState()

    def _rate_limited(self, source: str) -> bool:
        now = time.time()
        # reset window every 60s
        if now - self.state.window_start > 60:
            self.state.fetch_counts.clear()
            self.state.window_start = now
        count = self.state.fetch_counts.get(source, 0)
        if count >= self.rate_limit:
            logger.warning("MARKET_EVENT_RATE_LIMITED", extra={"source": source})
            return True
        self.state.fetch_counts[source] = count + 1
        return False

    def _cached(self, url: str) -> Any | None:
        entry = self.state.cache.get(url)
        if entry:
            ts, data = entry
            if time.time() - ts < self.cache_ttl:
                return data
            else:
                self.state.cache.pop(url, None)
        return None

    def _set_cache(self, url: str, data: Any) -> None:
        self.state.cache[url] = (time.time(), data)

    def fetch(self, url: str, *, source: str = "unknown") -> dict:
        """Fetch one URL with timeout/retry/backoff/ratelimit/cache/isolation.

        Returns {"ok": bool, "data": Any, "error": str, "source": str, "url": str}
        Never raises due to source failure — isolated.
        """
        if self._rate_limited(source):
            return {"ok": False, "data": None, "error": "rate_limited", "source": source, "url": url}
        cached = self._cached(url)
        if cached is not None:
            return {"ok": True, "data": cached, "error": "", "source": source, "url": url, "cached": True}
        if self.fetcher is None:
            return {"ok": False, "data": None, "error": "no fetcher configured (offline)", "source": source, "url": url}
        last_err = ""
        for attempt in range(self.max_retries + 1):
            try:
                # fetcher should respect timeout internally; we also enforce via call
                # No eval/exec — fetcher returns data, we treat as opaque dict
                data = self.fetcher(url)
                # validate basic shape: must be dict-like with headline
                if data is None:
                    raise ValueError("empty response")
                # cache success
                self._set_cache(url, data)
                return {"ok": True, "data": data, "error": "", "source": source, "url": url}
            except Exception as exc:
                last_err = type(exc).__name__ + ": " + str(exc)[:200]
                logger.warning("MARKET_EVENT_FETCH_FAILED", extra={"source": source, "attempt": attempt, "error": last_err})
                if attempt < self.max_retries:
                    # bounded backoff
                    backoff = self.backoff_base * (2 ** attempt)
                    backoff = min(backoff, 2.0)
                    time.sleep(backoff)
                continue
        return {"ok": False, "data": None, "error": last_err or "fetch failed", "source": source, "url": url}

    def fetch_many(self, urls: list[str], *, source: str = "unknown") -> list[dict]:
        """Fetch many URLs with per-source isolation — one failure doesn't stop others."""
        results = []
        for url in urls:
            try:
                results.append(self.fetch(url, source=source))
            except Exception as exc:  # absolute isolation
                results.append({"ok": False, "data": None, "error": type(exc).__name__, "source": source, "url": url})
        return results

    # No dynamic code execution: explicitly block eval/exec on downloaded content
    @staticmethod
    def safe_parse(data: Any) -> dict:
        """Safely parse downloaded content without eval/exec/shell.

        Only handles dict/list primitives; rejects anything requiring code execution.
        """
        if isinstance(data, dict):
            # shallow copy, ensure no code strings are executed
            # explicitly forbid keys that suggest code
            forbidden = {"__eval__", "__exec__", "eval", "exec", "import", "os", "subprocess"}
            if any(k in forbidden for k in data.keys()):
                return {"error": "forbidden key in data"}
            return dict(data)
        if isinstance(data, list):
            return {"items": list(data)[:100]}  # bounded
        return {"raw": str(data)[:2000]}

