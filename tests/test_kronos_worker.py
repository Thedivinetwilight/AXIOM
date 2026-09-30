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
from pathlib import Path

import pytest

from axiom.forecast.pipeline import ForecastPipeline
from axiom.forecast.store import ForecastStore
from axiom.kronos.backends import KronosInference, StubKronosBackend
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
ROOT = Path(__file__).resolve().parents[1]


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


class TestProtocolBoundaryStrictness:
    """The wire is a process boundary: naive stamps and junk numbers must fail.

    ``datetime.fromisoformat`` accepts naive timestamps, and a naive/aware
    comparison raises ``TypeError`` deep inside the pipeline — so the boundary
    refuses them up front with a message that names the field.
    """

    def test_a_naive_bar_timestamp_is_refused(self):
        rows = [["2024-01-01T00:00:00", 1.0, 2.0, 0.5, 1.5, 10.0, "DEMO"]]
        with pytest.raises(ProtocolError, match="has no timezone offset"):
            decode_bars(rows)

    def test_a_naive_future_timestamp_is_refused(self):
        with pytest.raises(ProtocolError, match="has no timezone offset"):
            decode_timestamps(["2024-01-01T00:00:00"])

    def test_an_unparseable_timestamp_names_the_field(self):
        rows = [["yesterday", 1.0, 2.0, 0.5, 1.5, 10.0, "DEMO"]]
        with pytest.raises(ProtocolError, match="bar timestamp is not an ISO-8601"):
            decode_bars(rows)

    def test_aware_timestamps_pass_through_unchanged(self):
        stamp = datetime(2024, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        rows = [[stamp.isoformat(), 1.0, 2.0, 0.5, 1.5, 10.0, "DEMO"]]
        assert decode_bars(rows)[0].timestamp == stamp

    @pytest.mark.parametrize("bad", [True, False, "abc", None, [], {}])
    def test_non_numeric_prices_are_refused(self, bad):
        rows = [["2024-01-01T00:00:00Z", bad, 2.0, 0.5, 1.5, 10.0, "DEMO"]]
        with pytest.raises(ProtocolError, match="open is not a number"):
            decode_bars(rows)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "NaN"])
    def test_non_finite_prices_are_refused(self, bad):
        rows = [["2024-01-01T00:00:00Z", 1.0, 2.0, 0.5, bad, 10.0, "DEMO"]]
        with pytest.raises(ProtocolError, match="close is not finite"):
            decode_bars(rows)


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


class TestChildSurvivesHostileStdin:
    """The child reads a pipe it does not control; junk must not crash it.

    Run against a genuinely spawned ``python -m axiom.worker.child`` process, so
    this exercises the real stdin loop, not just ``ChildRuntime.handle``.
    """

    ATTACKS = [
        "not json at all",
        "{}",
        '{"kind":"ping"}',                                   # missing id
        '{"kind":"place_order","id":"1"}',                   # trading verb
        '{"kind":"set_risk_limit","id":"1"}',
        '{"kind":"ping","id":"1","payload":[1,2]}',          # payload not an object
        '{"kind":"forecast","id":"1","payload":{"bars":"nope"}}',
        '{"kind":"forecast","id":"1","payload":{"bars":[["yesterday",1,2,0.5,1.5,10,'
        '"DEMO"]],"future_timestamps":["2024-01-01T01:00:00+00:00"]}}',
        '{"kind":"forecast","id":"1","payload":{"bars":[["2024-01-01T00:00:00+00:00",'
        'true,2,0.5,1.5,10,"DEMO"]],"future_timestamps":["2024-01-01T01:00:00+00:00"]}}',
    ]

    def test_junk_frames_are_refused_and_the_child_keeps_serving(self):
        frames = "\n".join(self.ATTACKS) + "\n" + \
            '{"kind":"shutdown","id":"done"}\n'
        completed = subprocess.run(  # noqa: S603 - fixed interpreter/module
            [sys.executable, "-m", "axiom.worker.child"],
            input=frames, capture_output=True, text=True, timeout=120, cwd=str(ROOT))

        assert completed.returncode == 0, completed.stderr[-1500:]
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        assert responses, "the child answered nothing"
        assert responses[-1]["id"] == "done" and responses[-1]["ok"] is True, \
            "the child did not reach a clean shutdown"
        # Every hostile frame produced an explicit refusal, never a crash.
        for response in responses[:-1]:
            assert response["ok"] is False, response

    def test_no_trading_verb_is_accepted(self):
        completed = subprocess.run(  # noqa: S603 - fixed interpreter/module
            [sys.executable, "-m", "axiom.worker.child"],
            input='{"kind":"place_order","id":"x"}\n{"kind":"shutdown","id":"z"}\n',
            capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        # The frame is rejected during decode, before its id is trusted, so the
        # refusal carries the placeholder id "?" rather than the caller's.
        refused = [r for r in responses if "unknown request kind" in (r.get("error") or "")]
        assert len(refused) == 1, responses
        assert "place_order" in refused[0]["error"]
        assert refused[0]["ok"] is False
        # The child stayed alive and still honoured the clean shutdown.
        shutdown = next(r for r in responses if r["id"] == "z")
        assert shutdown["ok"] is True and shutdown["state"] == "OFFLINE"

    def test_a_valid_sequence_still_works_after_the_junk(self):
        """Recovery matters as much as survival."""
        cfg = json.dumps({
            "enabled": True, "backend": "stub", "lookback": 64, "pred_len": 4,
            "min_history": 16, "sample_paths": 1})
        bars = [[b.timestamp.isoformat(), b.open, b.high, b.low, b.close, b.volume,
                 b.instrument] for b in window(64)]
        future = [s.isoformat() for s in stamps(4, start=window(64)[-1].timestamp
                                                + timedelta(hours=1))]
        frames = "\n".join([
            "garbage",
            '{"kind":"load","id":"load","payload":{"config":' + cfg + "}}",
            json.dumps({"kind": "forecast", "id": "fc",
                        "payload": {"bars": bars, "future_timestamps": future}}),
            '{"kind":"shutdown","id":"bye"}',
        ]) + "\n"
        completed = subprocess.run(  # noqa: S603 - fixed interpreter/module
            [sys.executable, "-m", "axiom.worker.child"],
            input=frames, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        assert completed.returncode == 0, completed.stderr[-1500:]
        by_id = {r["id"]: r for r in
                 (json.loads(line) for line in completed.stdout.splitlines() if line.strip())}
        assert by_id["load"]["ok"] is True, by_id["load"].get("error")
        assert by_id["load"]["state"] == "READY"
        assert by_id["fc"]["ok"] is True, by_id["fc"].get("error")
        assert len(by_id["fc"]["result"]["paths"][0]) == 4
        assert by_id["bye"]["ok"] is True

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


class TestConfigurationIsNeverSilentlyDowngraded:
    """A bad setting must fail, never quietly become something else.

    The dangerous failure mode for this subsystem is a misconfigured deployment
    that appears to work: a typo'd backend silently becoming the deterministic
    stub, or a malformed number silently becoming a default, would produce
    plausible-looking forecasts from the wrong model with nothing in the UI to
    show it. ``kronos_config_from_env`` had no test coverage at all.
    """

    ENABLED = {"AXIOM_KRONOS_ENABLED": "true"}

    def test_the_default_backend_is_upstream_never_stub(self):
        from axiom.bootstrap import kronos_config_from_env

        assert kronos_config_from_env(self.ENABLED).backend == "upstream"

    @pytest.mark.parametrize("value", ["magic", "STUB", "Stub", "upstream ", "stub "])
    def test_an_unknown_backend_is_rejected_not_defaulted(self, value):
        from axiom.bootstrap import kronos_config_from_env

        with pytest.raises(ValueError, match="backend must be"):
            kronos_config_from_env({**self.ENABLED, "AXIOM_KRONOS_BACKEND": value})

    @pytest.mark.parametrize("key", [
        "AXIOM_KRONOS_BACKEND", "AXIOM_KRONOS_LOOKBACK", "AXIOM_KRONOS_MAX_CONTEXT",
    ])
    def test_an_empty_value_means_unset_and_takes_the_safe_default(self, key):
        """An empty env var is 'not configured', not a value to coerce.

        The direction matters: an unset backend resolves to the REAL backend, so
        a blank setting can never quietly select the deterministic stub.
        """
        from axiom.bootstrap import kronos_config_from_env

        cfg = kronos_config_from_env({**self.ENABLED, key: ""})
        assert cfg.backend == "upstream"
        assert (cfg.lookback, cfg.max_context) == (400, 512)

    @pytest.mark.parametrize("key,value", [
        ("AXIOM_KRONOS_LOOKBACK", "lots"),
        ("AXIOM_KRONOS_LOOKBACK", "12.5"),
        ("AXIOM_KRONOS_PRED_LEN", "eight"),
        ("AXIOM_KRONOS_SAMPLE_PATHS", "2.7"),
        ("AXIOM_KRONOS_MAX_CONTEXT", "wide"),
    ])
    def test_a_malformed_integer_is_rejected_not_coerced(self, key, value):
        from axiom.bootstrap import kronos_config_from_env

        with pytest.raises(ValueError, match="must be an integer"):
            kronos_config_from_env({**self.ENABLED, key: value})

    @pytest.mark.parametrize("key,value,match", [
        ("AXIOM_KRONOS_PRED_LEN", "-5", "pred_len"),
        ("AXIOM_KRONOS_PRED_LEN", "0", "pred_len"),
        ("AXIOM_KRONOS_STALE_AFTER", "0", "stale_after"),
        ("AXIOM_KRONOS_STALE_AFTER", "-1", "stale_after"),
        ("AXIOM_KRONOS_MIN_HISTORY", "-3", "min_history"),
    ])
    def test_an_out_of_range_number_is_rejected(self, key, value, match):
        from axiom.bootstrap import kronos_config_from_env

        with pytest.raises(ValueError, match=match):
            kronos_config_from_env({**self.ENABLED, key: value})

    def test_a_valid_configuration_is_honoured_exactly(self):
        """The other half: strictness must not mean the values are ignored."""
        from axiom.bootstrap import kronos_config_from_env

        cfg = kronos_config_from_env({
            **self.ENABLED,
            "AXIOM_KRONOS_BACKEND": "stub",
            "AXIOM_KRONOS_LOOKBACK": "128",
            "AXIOM_KRONOS_PRED_LEN": "16",
            "AXIOM_KRONOS_SAMPLE_PATHS": "5",
            "AXIOM_KRONOS_MIN_HISTORY": "40",
            "AXIOM_KRONOS_STALE_AFTER": "900",
            "AXIOM_KRONOS_MODEL_REVISION": "901c26c1",
        })
        assert cfg.backend == "stub"
        assert (cfg.lookback, cfg.pred_len, cfg.sample_paths) == (128, 16, 5)
        assert (cfg.min_history, cfg.stale_after_seconds) == (40, 900.0)
        assert cfg.model_revision == "901c26c1"

    def test_an_invalid_mode_falls_back_openly_not_silently(self):
        """Mode is the one setting that does fall back — so it must say so."""
        from axiom.bootstrap import build_forecast_service

        environment = build_forecast_service({
            **self.ENABLED,
            "AXIOM_KRONOS_BACKEND": "stub",
            "AXIOM_KRONOS_MODE": "telepathy",
            "AXIOM_MARKET_DATA_SOURCE": "replay",
            "AXIOM_MARKET_DATA_CSV": SAMPLE_CSV,
            "AXIOM_MARKET_DATA_TIMEFRAME": "1h",
            "AXIOM_FORECAST_SERIES": "DEMO:1h",
        })
        try:
            assert environment.available
            assert any("AXIOM_KRONOS_MODE invalid" in note for note in environment.notes), \
                environment.notes
        finally:
            if environment.service is not None:
                environment.service.stop(timeout=10)

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


class TestServiceErrorPropagation:
    """A failing manager must degrade the service, never kill it or the GUI.

    These are regression tests for four real defects found by audit: an
    unguarded manager call in each of the worker startup, the GUI-thread status
    read, the worker-loop status refresh and teardown. The startup and refresh
    ones were the serious pair — either killed the worker thread, so the UI
    queued refreshes forever with no outcome and no reason shown.
    """

    class ExplodingManager:
        """Every manager entry point raises, as a wedged child could."""

        def __init__(self) -> None:
            self.load_calls = 0
            self.status_calls = 0

        def load(self):
            self.load_calls += 1
            raise RuntimeError("manager blew up at startup")

        def status(self):
            self.status_calls += 1
            raise RuntimeError("status blew up")

        def cached_status(self):
            raise RuntimeError("cached_status blew up")

        def shutdown(self, timeout: float = 10.0):
            raise RuntimeError("shutdown blew up")

    def _service(self, manager):
        cfg = config(sample_paths=1)
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        pipeline = ForecastPipeline(
            provider, cfg, store=ForecastStore(),
            infer=lambda bars, future: KronosInference(
                (tuple({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                        "volume": 1.0} for _ in future),),
                1.0, "stub"))
        return ForecastService(pipeline, manager=manager)

    def test_a_startup_load_failure_is_reported_instead_of_killing_the_worker(self):
        manager = self.ExplodingManager()
        service = self._service(manager)
        try:
            service.start()
            deadline = time.monotonic() + 5
            while service.status().state is KronosState.DISABLED and \
                    time.monotonic() < deadline:
                time.sleep(0.02)
            assert service.running, "the worker thread must survive a load failure"
            status = service.status()
            assert status.state is KronosState.ERROR
            assert "Kronos load failed" in status.message
            assert "manager blew up at startup" in status.last_error
            assert manager.load_calls == 1
        finally:
            service.stop(timeout=5)

    def test_jobs_still_produce_outcomes_after_a_startup_failure(self):
        """The UI must get an answer, not an eternal spinner."""
        service = self._service(self.ExplodingManager())
        try:
            service.start()
            assert service.request_refresh("DEMO", "1h") is True
            deadline = time.monotonic() + 5
            outcomes = []
            while not outcomes and time.monotonic() < deadline:
                outcomes = service.poll()
                time.sleep(0.02)
            assert outcomes, "no outcome was delivered to the UI"
            assert service.running, "the worker thread died mid-loop"
            # The job must have actually RUN. Asserting only "an outcome
            # arrived" would also pass if the infer callable had blown up, since
            # the pipeline converts any exception into an ERROR outcome.
            assert outcomes[0].state is KronosState.READY, outcomes[0].message
            assert outcomes[0].snapshot is not None
        finally:
            service.stop(timeout=5)

    def test_status_from_the_gui_thread_never_raises(self):
        service = self._service(self.ExplodingManager())
        service.start()
        try:
            status = service.status()  # must not propagate into the Qt thread
            assert status is not None
        finally:
            service.stop(timeout=5)

    def test_a_failed_status_refresh_does_not_lose_the_forecast(self):
        """The refresh ran before the outcome was queued, so it could drop it."""
        manager = self.ExplodingManager()
        cfg = config(sample_paths=1)
        provider = ReplayMarketDataProvider.from_csv(SAMPLE_CSV, "1h")
        runtime = KronosRuntime(cfg, backend=StubKronosBackend())
        runtime.load()
        pipeline = ForecastPipeline(provider, cfg, store=ForecastStore(),
                                    runtime=runtime)
        service = ForecastService(pipeline, manager=manager)
        try:
            service.start()
            service.request_refresh("DEMO", "1h")
            deadline = time.monotonic() + 5
            outcomes = []
            while not outcomes and time.monotonic() < deadline:
                outcomes = service.poll()
                time.sleep(0.02)
            assert outcomes, "the completed forecast was lost"
            assert outcomes[0].state is KronosState.READY, outcomes[0].message
            assert service.running
        finally:
            service.stop(timeout=5)

    def test_stop_is_safe_when_shutdown_fails(self):
        service = self._service(self.ExplodingManager())
        service.start()
        service.stop(timeout=5)  # must not raise into the closing GUI thread
        assert service.running is False
