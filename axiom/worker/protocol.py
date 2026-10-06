"""IPC protocol between AXIOM and the AXIOM-Kronos process.

One JSON object per line over the child's stdin/stdout. Deliberately tiny and
explicit: the Kronos process can only load the model, report status, run
inference and shut down. There is no request kind that could place an order,
change a risk limit or touch Arena state — the protocol simply does not carry
such a concept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any

REQUEST_KINDS = frozenset({"ping", "load", "status", "forecast", "shutdown"})


class ProtocolError(ValueError):
    """A malformed IPC frame."""


@dataclass(frozen=True)
class KronosRequest:
    kind: str
    request_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in REQUEST_KINDS:
            raise ProtocolError(f"unknown request kind: {self.kind!r}")

    def encode(self) -> str:
        return json.dumps(
            {"kind": self.kind, "id": self.request_id, "payload": self.payload},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def decode(cls, line: str) -> "KronosRequest":
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"request is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProtocolError("request must be a JSON object")
        kind = raw.get("kind")
        request_id = raw.get("id")
        if not isinstance(kind, str) or not isinstance(request_id, str):
            raise ProtocolError("request needs string 'kind' and 'id'")
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            raise ProtocolError("request payload must be an object")
        return cls(kind=kind, request_id=request_id, payload=payload)


@dataclass(frozen=True)
class KronosResponse:
    request_id: str
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    state: str = ""

    def encode(self) -> str:
        return json.dumps(
            {
                "id": self.request_id,
                "ok": self.ok,
                "state": self.state,
                "error": self.error,
                "result": self.result,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @classmethod
    def decode(cls, line: str) -> "KronosResponse":
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"response is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise ProtocolError("response must be a JSON object with a string 'id'")
        result = raw.get("result", {})
        if not isinstance(result, dict):
            raise ProtocolError("response result must be an object")
        ok = raw.get("ok", False)
        if type(ok) is not bool:
            raise ProtocolError("response 'ok' must be a boolean")
        error = raw.get("error", "")
        state = raw.get("state", "")
        if not isinstance(error, str) or not isinstance(state, str):
            raise ProtocolError("response 'error' and 'state' must be strings")
        return cls(
            request_id=raw["id"],
            ok=ok,
            result=result,
            error=error,
            state=state,
        )

    @classmethod
    def failure(cls, request_id: str, error: str, state: str = "ERROR") -> "KronosResponse":
        return cls(request_id=request_id, ok=False, error=error, state=state)


# -- payload helpers (bars cross the IPC boundary as plain JSON) --------------
def encode_bars(bars) -> list[list[Any]]:
    return [
        [
            bar.timestamp.isoformat(),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.instrument,
        ]
        for bar in bars
    ]


def decode_timestamp(value: Any, *, field: str = "timestamp") -> datetime:
    """Parse one IPC timestamp, refusing anything naive or unparseable.

    ``datetime.fromisoformat`` accepts naive stamps, and a naive timestamp
    compared against an aware one raises ``TypeError`` — or worse, silently
    orders candles wrongly downstream. The wire format always carries an
    offset, so a naive value here means a corrupted or hostile frame.
    """
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolError(
            f"{field} has no timezone offset: {value!r} (the wire format requires one)"
        )
    return parsed.astimezone(timezone.utc)


def decode_number(value: Any, *, field: str) -> float:
    """Coerce one IPC field to a finite float, refusing bools and non-finites."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ProtocolError(f"{field} is not a number: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{field} is not a number: {value!r}") from exc
    if not isfinite(number):
        raise ProtocolError(f"{field} is not finite: {value!r}")
    return number


def decode_bars(rows: Any):
    from axiom.market.provider import MarketDataBar

    if not isinstance(rows, list):
        raise ProtocolError("bars must be a list")
    bars = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 7:
            raise ProtocolError(f"bar needs exactly [ts, o, h, l, c, v, instrument]: {row!r}")
        if (not isinstance(row[6], str) or not row[6].strip()
                or row[6].strip() != row[6]):
            raise ProtocolError(
                f"bar instrument must be a non-empty trimmed string: {row[6]!r}")
        bars.append(
            MarketDataBar(
                timestamp=decode_timestamp(row[0], field="bar timestamp"),
                open=decode_number(row[1], field="open"),
                high=decode_number(row[2], field="high"),
                low=decode_number(row[3], field="low"),
                close=decode_number(row[4], field="close"),
                volume=decode_number(row[5], field="volume"),
                instrument=str(row[6]),
            )
        )
    return bars


def encode_timestamps(stamps) -> list[str]:
    return [stamp.isoformat() for stamp in stamps]


def decode_timestamps(rows: Any) -> list[datetime]:
    if not isinstance(rows, list) or not rows:
        raise ProtocolError("future timestamps must be a non-empty list")
    return [decode_timestamp(row, field="future timestamp") for row in rows]
