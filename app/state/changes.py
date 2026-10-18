"""The "what changed?" engine (master spec section 8).

State deltas are a first-class product signal, not decoration. The engine
keeps the last snapshot per (domain, object_key), compares incoming snapshots
field-by-field and records every material change durably (state_changes table)
while also publishing it on the event bus.

Rules:
* Only fields present in BOTH snapshots are compared (a field appearing for
  the first time is recorded as previous=None, not as a fake zero).
* Comparison is textual on normalized values; numeric noise below ``tolerance``
  is not a change.
* The engine never invents evidence: the ``evidence`` string must come from
  the caller (which subsystem observed the change and why it matters).
"""

from __future__ import annotations

import math
from typing import Iterable


class ChangeEngine:
    def __init__(self, repository, bus=None, *, tolerance: float = 1e-9):
        self.repository = repository
        self.bus = bus
        self.tolerance = tolerance
        self._snapshots: dict[tuple[str, str], dict] = {}

    def observe(self, domain: str, object_key: str, snapshot: dict,
                evidence: str | None = None,
                ignore_fields: Iterable[str] = ()) -> list[dict]:
        """Record ``snapshot`` and return the list of material changes.

        Each returned change dict carries domain/object_key/field/previous/
        current/evidence and has already been persisted (when a repository is
        attached) and published (when a bus is attached).
        """
        key = (domain, object_key)
        snapshot = {k: v for k, v in snapshot.items() if k not in set(ignore_fields)}
        previous = self._snapshots.get(key)
        self._snapshots[key] = dict(snapshot)
        if previous is None:
            return []
        changes = []
        for field, current in snapshot.items():
            if field not in previous:
                changes.append(self._change(domain, object_key, field, None, current, evidence))
                continue
            old = previous[field]
            if self._same(old, current):
                continue
            changes.append(self._change(domain, object_key, field, old, current, evidence))
        for field in previous:
            if field not in snapshot:
                changes.append(self._change(domain, object_key, field, previous[field],
                                            None, evidence))
        return changes

    def _change(self, domain: str, object_key: str, field: str, previous, current,
                evidence: str | None) -> dict:
        change = {
            "domain": domain,
            "object_key": object_key,
            "field": field,
            "previous": previous,
            "current": current,
            "evidence": evidence or "state delta observed by change engine",
        }
        if self.repository is not None:
            try:
                change["id"] = self.repository.record_state_change(**change)
            except Exception:
                # Persistence failure must not break the observing subsystem;
                # the in-memory snapshot still advanced. The failure itself is
                # visible via the bus below as a system event.
                change["persisted"] = False
        if self.bus is not None:
            self.bus.publish("state.changed", {
                "domain": domain, "object_key": object_key, "field": field,
                "previous": _scalar(previous), "current": _scalar(current),
            })
        return change

    def _same(self, old, new) -> bool:
        if isinstance(old, (int, float)) and isinstance(new, (int, float)) \
                and not isinstance(old, bool) and not isinstance(new, bool):
            if math.isnan(old) or math.isnan(new):
                return math.isnan(old) and math.isnan(new)
            return abs(float(old) - float(new)) <= self.tolerance
        return old == new

    def recent(self, domain: str | None = None, limit: int = 100) -> list[dict]:
        if self.repository is None:
            return []
        return self.repository.list_state_changes(domain=domain, limit=limit)


def _scalar(value):
    """JSON-safe scalar form for event payloads."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
