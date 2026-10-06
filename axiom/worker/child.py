"""AXIOM-Kronos child process entry point.

    python -m axiom.worker.child

Reads ``KronosRequest`` JSON lines on stdin, answers ``KronosResponse`` lines on
stdout, logs on stderr. This process is the only one that imports PyTorch/Kronos,
so a model crash, an OOM or a long inference never touches the GUI process or the
Arena worker.

The child cannot do anything else: it has no market data provider, no store, no
network client and no path to Arena. It answers exactly four things — ping, load,
status, forecast — plus shutdown.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Iterable
from typing import IO, Any

from axiom.kronos.model_cache import ModelCache
from axiom.kronos.runtime import KronosRuntime
from axiom.market.provider import MarketDataError
from axiom.models import KronosConfig, KronosState
from axiom.worker.protocol import (
    KronosRequest,
    KronosResponse,
    ProtocolError,
    decode_bars,
    decode_timestamps,
)


class ChildRuntime:
    """Request dispatcher for the Kronos process."""

    def __init__(self, config: KronosConfig | None = None, *, cache: ModelCache | None = None,
                 runtime: KronosRuntime | None = None) -> None:
        self.runtime = runtime
        self.default_config = config
        self.cache = cache

    # -- handlers ------------------------------------------------------------
    def handle(self, request: KronosRequest) -> KronosResponse:
        try:
            if request.kind == "ping":
                return KronosResponse(
                    request.request_id, True, {"pong": True}, state=self._state()
                )
            if request.kind == "load":
                return self._load(request)
            if request.kind == "status":
                return self._status(request)
            if request.kind == "forecast":
                return self._forecast(request)
            if request.kind == "shutdown":
                if self.runtime is not None:
                    self.runtime.shutdown()
                return KronosResponse(
                    request.request_id, True, {"shutdown": True}, state=KronosState.OFFLINE.value
                )
        except (ProtocolError, MarketDataError, ValueError) as exc:
            return KronosResponse.failure(request.request_id, str(exc), self._state())
        except Exception as exc:  # keep the child alive for the next request
            print(
                "kronos child error: " + "".join(traceback.format_exception(exc))[-2000:],
                file=sys.stderr,
                flush=True,
            )
            return KronosResponse.failure(
                request.request_id, f"{type(exc).__name__}: {exc}", self._state()
            )
        raise ProtocolError(f"unhandled request kind: {request.kind}")

    def _state(self) -> str:
        return self.runtime.status.state.value if self.runtime else KronosState.DISABLED.value

    def _ensure_runtime(self, payload: dict[str, Any], *, kind: str = "load") -> KronosRuntime:
        config_payload = payload.get("config", {})
        if not isinstance(config_payload, dict):
            raise ValueError("Kronos request config must be an object")
        if self.runtime is not None:
            if config_payload:
                requested = KronosConfig.from_dict(config_payload)
                if requested != self.runtime.config:
                    raise ValueError(
                        "Kronos request configuration differs from the loaded runtime")
            return self.runtime
        if not config_payload and self.default_config is None:
            if kind == "load":
                raise ValueError("no Kronos configuration supplied with 'load'")
            raise ValueError(
                f"'{kind}' before 'load': the Kronos runtime is not configured. "
                "Send 'load' first, or include a 'config' with this request."
            )
        config = (
            self.default_config
            if not config_payload
            else KronosConfig.from_dict(config_payload)
        )
        self.runtime = KronosRuntime(
            config, cache=self.cache or ModelCache(config.cache_dir or None))
        return self.runtime

    def _load(self, request: KronosRequest) -> KronosResponse:
        runtime = self._ensure_runtime(request.payload, kind="load")
        status = runtime.load()
        return KronosResponse(
            request.request_id,
            status.state is KronosState.READY,
            {"status": status.to_dict()},
            error="" if status.state is KronosState.READY else status.message,
            state=status.state.value,
        )

    def _status(self, request: KronosRequest) -> KronosResponse:
        if self.runtime is None:
            status_payload = {"state": KronosState.DISABLED.value, "message": "not loaded"}
            return KronosResponse(request.request_id, True, {"status": status_payload},
                                  state=KronosState.DISABLED.value)
        status = self.runtime.status
        return KronosResponse(
            request.request_id, True, {"status": status.to_dict()}, state=status.state.value
        )

    def _forecast(self, request: KronosRequest) -> KronosResponse:
        runtime = self._ensure_runtime(request.payload, kind="forecast")
        payload = request.payload
        bars = decode_bars(payload.get("bars"))
        future_timestamps = decode_timestamps(payload.get("future_timestamps"))
        inference = runtime.forecast_paths(bars, future_timestamps)
        return KronosResponse(
            request.request_id,
            True,
            {
                "paths": [[dict(step) for step in path] for path in inference.paths],
                "latency_ms": inference.latency_ms,
                "backend": inference.backend,
                "notes": list(inference.notes),
                "model_id": inference.model_id,
                "model_revision": inference.model_revision,
                "status": runtime.status.to_dict(),
            },
            state=runtime.status.state.value,
        )

    # -- loop ----------------------------------------------------------------
    def serve(self, stdin: IO[str], stdout: IO[str], lines: Iterable[str] | None = None) -> int:
        source = lines if lines is not None else stdin
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                request = KronosRequest.decode(line)
            except ProtocolError as exc:
                self._write(stdout, KronosResponse.failure("?", str(exc)))
                continue
            response = self.handle(request)
            self._write(stdout, response)
            if request.kind == "shutdown":
                return 0
        return 0

    @staticmethod
    def _write(stdout: IO[str], response: KronosResponse) -> None:
        stdout.write(response.encode() + "\n")
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config: KronosConfig | None = None
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            try:
                config = KronosConfig.from_dict(json.loads(argv[index + 1]))
            except (ValueError, json.JSONDecodeError) as exc:
                print(f"invalid --config JSON: {exc}", file=sys.stderr)
                return 2
    child = ChildRuntime(
        config, cache=ModelCache(config.cache_dir or None) if config is not None else None)
    return child.serve(sys.stdin, sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
