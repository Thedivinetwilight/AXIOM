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
from datetime import datetime
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
        payload = raw.get("payload") or {}
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
        if not isinstance(raw, dict) or "id" not in raw:
            raise ProtocolError("response must be a JSON object with an 'id'")
        result = raw.get("result") or {}
        if not isinstance(result, dict):
            raise ProtocolError("response result must be an object")
        return cls(
            request_id=str(raw["id"]),
            ok=bool(raw.get("ok")),
            result=result,
            error=str(raw.get("error", "")),
            state=str(raw.get("state", "")),
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


def decode_bars(rows: Any):
    from axiom.market.provider import MarketDataBar

    if not isinstance(rows, list):
        raise ProtocolError("bars must be a list")
    bars = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            raise ProtocolError(f"bar needs [ts, o, h, l, c, v, instrument]: {row!r}")
        bars.append(
            MarketDataBar(
                timestamp=datetime.fromisoformat(str(row[0])),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                instrument=str(row[6]),
            )
        )
    return bars


def encode_timestamps(stamps) -> list[str]:
    return [stamp.isoformat() for stamp in stamps]


def decode_timestamps(rows: Any) -> list[datetime]:
    if not isinstance(rows, list) or not rows:
        raise ProtocolError("future timestamps must be a non-empty list")
    return [datetime.fromisoformat(str(row)) for row in rows]
