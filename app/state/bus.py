"""Event gateway (master spec section 71).

One shared in-process bus between the core and every UI surface. The GUI
subscribes to topics instead of polling every object; backend subsystems
publish typed events. Every event carries type, timestamp, source, object
identity and a monotonic sequence number so consumers can detect dropped or
out-of-order delivery and reconcile.

The bus is deliberately tiny and synchronous-safe: publishing never raises on
subscriber errors (a broken subscriber must not break the trading path — the
failure is counted and surfaced via ``health()``).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Callable


class EventBus:
    def __init__(self, *, history: int = 500):
        self._lock = threading.RLock()
        self._sequence = 0
        self._subscribers: dict[str, list[Callable[[dict], None]]] = {}
        self._history: deque[dict] = deque(maxlen=history)
        self._dropped = 0
        self._errors = 0
        self._published = 0

    # ------------------------------------------------------------- publish --
    def publish(self, event_type: str, payload: dict, *, source: str = "core",
                object_key: str | None = None) -> dict:
        with self._lock:
            self._sequence += 1
            self._published += 1
            event = {
                "type": event_type,
                "seq": self._sequence,
                "ts": time.time(),
                "source": source,
                "object_key": object_key,
                "payload": payload if isinstance(payload, dict) else {"value": payload},
            }
            self._history.append(event)
            subscribers = list(self._subscribers.get(event_type, []))
            subscribers += list(self._subscribers.get("*", []))
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                with self._lock:
                    self._errors += 1
        return event

    # ----------------------------------------------------------- subscribe --
    def subscribe(self, event_type: str, callback: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

        def unsubscribe() -> None:
            with self._lock:
                subs = self._subscribers.get(event_type, [])
                if callback in subs:
                    subs.remove(callback)

        return unsubscribe

    def replay_since(self, sequence: int, event_type: str | None = None) -> list[dict]:
        """Events after ``sequence`` (reconciliation after a gap)."""
        with self._lock:
            events = [e for e in self._history if e["seq"] > sequence]
        if event_type:
            events = [e for e in events if e["type"] == event_type]
        return events

    def latest_sequence(self) -> int:
        with self._lock:
            return self._sequence

    # ------------------------------------------------------------ SSE aid --
    async def iterate(self, event_types: tuple[str, ...] = (), *,
                      poll_interval: float = 0.2, idle_timeout: float | None = None):
        """Async generator of events for SSE consumers.

        Implemented with a private queue so a slow HTTP client can never block
        publishers; events arriving while the queue is full are dropped and
        counted (the consumer reconciles via ``replay_since``).
        """
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()

        def _push(event: dict) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except asyncio.QueueFull:
                with self._lock:
                    self._dropped += 1
            except RuntimeError:
                with self._lock:
                    self._dropped += 1

        wanted = set(event_types)
        unsub = self.subscribe("*", lambda e: _push(e) if not wanted or e["type"] in wanted else None)
        started = time.time()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=poll_interval)
                    yield event
                except asyncio.TimeoutError:
                    if idle_timeout is not None and time.time() - started > idle_timeout:
                        return
                    # Keep the connection alive; the HTTP layer sends pings.
                    continue
        finally:
            unsub()

    # --------------------------------------------------------------- health --
    def health(self) -> dict:
        with self._lock:
            return {
                "published": self._published,
                "sequence": self._sequence,
                "subscribers": sum(len(v) for v in self._subscribers.values()),
                "history_depth": len(self._history),
                "dropped": self._dropped,
                "subscriber_errors": self._errors,
            }
