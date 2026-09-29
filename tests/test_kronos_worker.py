"""Worker isolation: IPC protocol, child process, manager, failure isolation.

Also covers the runtime guarantees the UI depends on: the GUI thread never
blocks on Kronos, Arena polling keeps running during inference, and a dead or
hanging Kronos process cannot take the terminal down.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from axiom.forecast.pipeline import ForecastPipeline
from axiom.forecast.store import ForecastStore
from axiom.kronos.backends import StubKronosBackend
from axiom.kronos.runtime import KronosRuntime
from axiom.market.replay import ReplayMarketDataProvider
from axiom.models import KronosConfig, KronosState
from axiom.service import ForecastService
from axiom.worker.child import ChildRuntime, main as child_main
from axiom.worker.manager import KronosManager, in_process_manager
from axiom.worker.protocol import (
    REQUEST_KINDS,
    KronosRequest,
    KronosResponse,
    ProtocolError,
    decode_bars,
    decode_timestamps,
    encode_bars,
    encode_timestamps,
)

UTC = timezone.utc
SAMPLE_CSV = "data/sample_1h.csv"


def config(**overrides) -> KronosConfig:
    base = dict(enabled=True, backend="stub", lookback=120, pred_len=8,
                min_history=32, sample_paths=1)
    base.update(overrides)
    return KronosConfig(**base).validate()


def window(count: int = 120):
    provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
    return list(provider.fetch("DEMO", "1h").bars)[:count]


def stamps(count: int, start: datetime | None = None):
    origin = start or datetime(2024, 1, 1, tzinfo=UTC)
    return [origin + timedelta(hours=index) for index in range(count)]


class TestProtocol:
    def test_request_round_trip(self):
        request = KronosRequest("forecast", "abc", {"pred_len": 8})
        restored = KronosRequest.decode(request.encode())
        assert restored == request

    def test_response_round_trip(self):
        response = KronosResponse("abc", True, {"paths": []}, state="READY")
        restored = KronosResponse.decode(response.encode())
        assert restored.request_id == "abc"
        assert restored.ok is True
        assert restored.state == "READY"

    def test_unknown_request_kinds_are_rejected(self):
        with pytest.raises(ProtocolError, match="unknown request kind"):
            KronosRequest("place_order", "abc")
        with pytest.raises(ProtocolError, match="unknown request kind"):
            KronosRequest.decode('{"kind": "set_risk_limit", "id": "x"}')

    def test_the_protocol_has_no_trading_surface(self):
        """The Kronos process cannot be told to trade, change risk or go live."""
        assert REQUEST_KINDS == {"ping", "load", "status", "forecast", "shutdown"}
        forbidden = {"order", "trade", "risk", "live", "stop", "resume", "account"}
        assert not (forbidden & {kind.lower() for kind in REQUEST_KINDS})

    def test_malformed_frames_are_rejected(self):
        with pytest.raises(ProtocolError):
            KronosRequest.decode("not json")
        with pytest.raises(ProtocolError):
            KronosRequest.decode('{"kind": "ping"}')
        with pytest.raises(ProtocolError):
            KronosResponse.decode("[1, 2, 3]")

    def test_bars_and_timestamps_round_trip(self):
        bars = window(3)
        restored = decode_bars(encode_bars(bars))
        assert [b.to_dict() for b in restored] == [b.to_dict() for b in bars]
        future = stamps(3)
        assert decode_timestamps(encode_timestamps(future)) == future

    def test_bad_bars_are_rejected(self):
        with pytest.raises(ProtocolError, match="bars must be a list"):
            decode_bars("nope")
        with pytest.raises(ProtocolError, match="bar needs"):
            decode_bars([[1, 2]])


class TestChildRuntime:
    def test_load_status_forecast_shutdown(self):
        child = ChildRuntime(config())
        loaded = child.handle(KronosRequest("load", "1"))
        assert loaded.ok and loaded.state == "READY"

        status = child.handle(KronosRequest("status", "2"))
        assert json.loads(status.encode())["result"]["status"]["state"] == "READY"

        forecast = child.handle(KronosRequest("forecast", "3", {
            "bars": encode_bars(window(64)),
            "future_timestamps": encode_timestamps(stamps(8)),
        }))
        assert forecast.ok
        assert len(forecast.result["paths"]) == 1
        assert len(forecast.result["paths"][0]) == 8

        shutdown = child.handle(KronosRequest("shutdown", "4"))
        assert shutdown.ok and shutdown.state == "OFFLINE"

    def test_forecast_before_load_is_refused_without_killing_the_child(self):
        child = ChildRuntime(config())
        refused = child.handle(KronosRequest("forecast", "1", {
            "bars": encode_bars(window(64)),
            "future_timestamps": encode_timestamps(stamps(8)),
        }))
        assert refused.ok is False
        assert "not ready" in refused.error
        # Still alive and usable afterwards.
        assert child.handle(KronosRequest("load", "2")).ok is True

    def test_serve_loop_survives_a_bad_frame(self):
        child = ChildRuntime(config())
        out = []

        class Sink:
            def write(self, text):
                out.append(text)

            def flush(self):
                pass

        lines = ["not json at all",
                 KronosRequest("ping", "ok").encode()]
        assert child.serve(None, Sink(), lines=lines) == 0
        assert len(out) == 2
        assert '"pong":true' in out[1]

    def test_shutdown_ends_the_loop(self):
        child = ChildRuntime(config())
        out = []

        class Sink:
            def write(self, text):
                out.append(text)

            def flush(self):
                pass

        child.serve(None, Sink(), lines=[KronosRequest("shutdown", "bye").encode(),
                                         KronosRequest("ping", "never").encode()])
        assert len(out) == 1, "no request is served after shutdown"

    def test_child_main_rejects_a_bad_config(self, capsys):
        assert child_main(["--config", "{not json"]) == 2
        assert "invalid --config JSON" in capsys.readouterr().err


class TestManagerInProcess:
    def test_disabled_config_never_starts_a_runtime(self):
        manager = KronosManager(KronosConfig(enabled=False), mode="in-process")
        status = manager.start()
        assert status.state is KronosState.DISABLED
        assert manager.alive is False
        assert manager.load().state is KronosState.DISABLED

    def test_full_cycle(self):
        manager = in_process_manager(config())
        assert manager.load().state is KronosState.READY
        assert manager.ping()
        inference = manager.forecast(window(64), stamps(8))
        assert inference.sample_paths == 1
        assert inference.pred_len == 8
        manager.shutdown()
        assert manager.alive is False
        assert manager.status().state is KronosState.OFFLINE

    def test_requests_after_shutdown_report_offline(self):
        manager = in_process_manager(config())
        manager.load()
        manager.shutdown()
        with pytest.raises(Exception):
            manager.forecast(window(64), stamps(8))

    def test_invalid_mode_is_rejected(self):
        with pytest.raises(ValueError, match="mode must be"):
            KronosManager(config(), mode="telepathy")


class TestManagerProcess:
    """The packaged AXIOM-Kronos runtime, spawned as a real subprocess."""

    def test_packaged_runtime_starts_and_serves(self):
        manager = KronosManager(config(), mode="process", capture_stderr=True)
        try:
            status = manager.load(timeout=120)
            assert status.state is KronosState.READY, manager.stderr_tail()
            assert manager.alive
            assert manager.status().pid > 0
            assert manager.ping(timeout=30)
            inference = manager.forecast(window(64), stamps(8), timeout=120)
            assert inference.pred_len == 8
            assert inference.backend == "stub"
        finally:
            manager.shutdown(timeout=30)
        assert manager.alive is False

    def test_child_process_is_configured_from_the_command_line(self):
        payload = json.dumps(config(pred_len=4).to_dict())
        command = [sys.executable, "-m", "axiom.worker.child", "--config", payload]
        process = subprocess.run(  # noqa: S603 - fixed interpreter and module
            command,
            input="\n".join([
                KronosRequest("load", "1").encode(),
                KronosRequest("status", "2").encode(),
                KronosRequest("shutdown", "3").encode(),
            ]) + "\n",
            capture_output=True, text=True, timeout=180, cwd=os.getcwd(),
        )
        assert process.returncode == 0, process.stderr[-2000:]
        lines = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        assert [line["id"] for line in lines] == ["1", "2", "3"]
        assert lines[0]["ok"] is True
        assert lines[1]["result"]["status"]["state"] == "READY"
        assert lines[2]["state"] == "OFFLINE"

    def test_clean_shutdown_leaves_no_process_behind(self):
        manager = KronosManager(config(), mode="process")
        manager.load(timeout=120)
        pid = manager.status().pid
        assert pid > 0
        manager.shutdown(timeout=30)
        # Give the OS a moment, then confirm the pid is gone.
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:  # pragma: no cover - only on a wedged OS
            pytest.fail(f"kronos child {pid} is still running after shutdown")
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_dead_kronos_process_is_isolated(self):
        manager = KronosManager(config(), mode="process")
        manager.load(timeout=120)
        pid = manager.status().pid
        os.kill(pid, 9)
        # The manager must notice and degrade, not raise into the caller.
        deadline = time.time() + 10
        status = manager.status()
        while status.state is not KronosState.OFFLINE and time.time() < deadline:
            time.sleep(0.1)
            status = manager.status()
        assert status.state is KronosState.OFFLINE
        assert manager.alive is False
        with pytest.raises(Exception):
            manager.forecast(window(64), stamps(8))
        manager.shutdown(timeout=10)

    def test_a_failing_load_is_reported_not_hidden(self, tmp_path, monkeypatch):
        # Point the child at a Kronos source that does not exist: the load must
        # come back as an explicit failure.
        broken = config(backend="upstream")
        manager = KronosManager(
            broken, mode="process", env={"AXIOM_KRONOS_SOURCE": str(tmp_path / "nope")})
        status = manager.load(timeout=120)
        assert status.state is KronosState.ERROR
        assert status.is_ready is False
        manager.shutdown(timeout=10)


class SlowBackend(StubKronosBackend):
    """Deliberately slow inference, to prove the GUI/Arena paths stay free."""

    kind = "stub-slow"

    def __init__(self, delay: float = 0.6):
        super().__init__()
        self.delay = delay
        self.calls = 0

    def forecast(self, bars, future_timestamps, cfg):
        self.calls += 1
        time.sleep(self.delay)
        return super().forecast(bars, future_timestamps, cfg)


class TestResponsiveness:
    def _service(self, delay: float = 0.6):
        cfg = config()
        runtime = KronosRuntime(cfg, backend=SlowBackend(delay))
        runtime.load()
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        pipeline = ForecastPipeline(provider, cfg, store=ForecastStore(),
                                    runtime=runtime)
        return ForecastService(pipeline), runtime

    def test_gui_calls_never_block_on_inference(self):
        service, runtime = self._service(delay=0.8)
        try:
            service.start()
            started = time.perf_counter()
            assert service.request_refresh("DEMO", "1h") is True
            queued_ms = (time.perf_counter() - started) * 1000
            assert queued_ms < 50, f"queueing took {queued_ms:.1f} ms"

            # Poll repeatedly while the model is busy: every poll returns fast.
            polls = 0
            deadline = time.perf_counter() + 0.5
            while time.perf_counter() < deadline:
                tick = time.perf_counter()
                service.poll()
                assert (time.perf_counter() - tick) * 1000 < 20
                polls += 1
                time.sleep(0.01)
            assert polls > 10

            deadline = time.perf_counter() + 5
            outcomes = []
            while not outcomes and time.perf_counter() < deadline:
                outcomes = service.poll()
                time.sleep(0.02)
            assert outcomes and outcomes[0].state is KronosState.READY
            assert runtime.backend.calls == 1
        finally:
            service.stop(timeout=10)

    def test_arena_polling_continues_during_inference(self):
        """A simulated Arena poll loop keeps ticking while Kronos infers."""
        service, runtime = self._service(delay=0.8)
        polls = []
        stop = threading.Event()

        def arena_poller():
            while not stop.is_set():
                polls.append(time.perf_counter())
                time.sleep(0.02)

        poller = threading.Thread(target=arena_poller, daemon=True)
        poller.start()
        try:
            service.start()
            service.request_refresh("DEMO", "1h")
            deadline = time.perf_counter() + 5
            outcomes = []
            while not outcomes and time.perf_counter() < deadline:
                outcomes = service.poll()
                time.sleep(0.02)
            assert outcomes, "inference never completed"
            before = len(polls)
            assert before > 5, "Arena polling stalled while Kronos was inferring"
            gaps = [b - a for a, b in zip(polls, polls[1:])]
            assert max(gaps) < 0.4, f"Arena polling was blocked for {max(gaps):.2f}s"
        finally:
            stop.set()
            poller.join(timeout=5)
            service.stop(timeout=10)

    def test_service_survives_a_pipeline_exception(self):
        cfg = config()
        runtime = KronosRuntime(cfg, backend=SlowBackend(0.0))
        runtime.load()
        pipeline = ForecastPipeline(
            ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h"), cfg,
            store=ForecastStore(), runtime=runtime)
        service = ForecastService(pipeline)

        def explode(*args, **kwargs):
            raise RuntimeError("unexpected")

        pipeline.run = explode  # type: ignore[method-assign]
        try:
            service.start()
            service.request_refresh("DEMO", "1h")
            deadline = time.perf_counter() + 5
            outcomes = []
            while not outcomes and time.perf_counter() < deadline:
                outcomes = service.poll()
                time.sleep(0.02)
            assert outcomes
            assert outcomes[0].state is KronosState.ERROR
            assert "forecast service error" in outcomes[0].message
            assert service.running, "the worker thread must survive a bug"
        finally:
            service.stop(timeout=10)

    def test_service_describe_is_json_friendly(self):
        service, _ = self._service(delay=0.0)
        try:
            service.start()
            service.request_refresh("DEMO", "1h")
            payload = service.describe()
            assert json.dumps(payload, default=str)
            assert payload["status"]["state"] in {state.value for state in KronosState}
        finally:
            service.stop(timeout=10)


class TestBootstrap:
    def test_kronos_disabled_by_default(self):
        from axiom.bootstrap import build_forecast_service

        environment = build_forecast_service({})
        assert environment.available is False
        assert environment.status.state is KronosState.DISABLED

    def test_explicit_enablement_with_replay_and_stub(self):
        from axiom.bootstrap import build_forecast_service

        environment = build_forecast_service({
            "AXIOM_KRONOS_ENABLED": "true",
            "AXIOM_KRONOS_BACKEND": "stub",
            "AXIOM_KRONOS_MODE": "in-process",
            "AXIOM_KRONOS_LOOKBACK": "120",
            "AXIOM_KRONOS_PRED_LEN": "8",
            "AXIOM_KRONOS_MIN_HISTORY": "32",
            "AXIOM_MARKET_DATA_SOURCE": "replay",
            "AXIOM_MARKET_DATA_CSV": SAMPLE_CSV,
            "AXIOM_MARKET_DATA_TIMEFRAME": "1h",
            "AXIOM_FORECAST_SERIES": "DEMO:1h",
        })
        try:
            assert environment.available
            assert tuple(environment.series) == (("DEMO", "1h"),)
            service = environment.service
            service.start()
            service.refresh_if_due(environment.series)
            deadline = time.perf_counter() + 20
            outcomes = []
            while not outcomes and time.perf_counter() < deadline:
                outcomes = service.poll()
                time.sleep(0.05)
            assert outcomes and outcomes[0].state is KronosState.READY
            assert service.latest("DEMO", "1h") is not None
        finally:
            environment.service.stop(timeout=10)

    def test_bad_configuration_is_reported_not_raised(self):
        from axiom.bootstrap import build_forecast_service

        environment = build_forecast_service({
            "AXIOM_KRONOS_ENABLED": "true",
            "AXIOM_KRONOS_LOOKBACK": "900",
            "AXIOM_KRONOS_MAX_CONTEXT": "512",
        })
        assert environment.available is False
        assert environment.status.state is KronosState.ERROR

    def test_missing_data_source_degrades(self):
        from axiom.bootstrap import build_forecast_service

        environment = build_forecast_service({"AXIOM_KRONOS_ENABLED": "true"})
        assert environment.available is False
        assert environment.status.state is KronosState.DATA_ERROR
        assert "AXIOM_MARKET_DATA_CSV" in environment.status.message

    def test_calendar_and_series_parsing(self):
        from axiom.bootstrap import calendar_from_env, series_from_env

        assert calendar_from_env({}).name == "continuous-24x7"
        assert calendar_from_env({"AXIOM_MARKET_CALENDAR": "daily"}).name == "daily"
        session = calendar_from_env({
            "AXIOM_MARKET_CALENDAR": "session",
            "AXIOM_MARKET_SESSION_OPEN": "09:15",
            "AXIOM_MARKET_SESSION_CLOSE": "15:30",
        })
        assert session.open_time.hour == 9 and session.open_time.minute == 15
        assert series_from_env({"AXIOM_FORECAST_SERIES": "DEMO:1h, BTCUSDT:5m"}) == [
            ("DEMO", "1h"), ("BTCUSDT", "5m")]
        with pytest.raises(ValueError, match="INSTRUMENT:TIMEFRAME"):
            series_from_env({"AXIOM_FORECAST_SERIES": "DEMO"})
        with pytest.raises(ValueError, match="AXIOM_MARKET_CALENDAR"):
            calendar_from_env({"AXIOM_MARKET_CALENDAR": "lunar"})
