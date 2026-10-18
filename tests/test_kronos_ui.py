"""FORECAST panel / MARKETS overlay: labelling, degradation, non-blocking UI.

Qt is not required: the view models and the controller are plain Python. The
desktop wiring itself is asserted structurally (the GUI cannot be instantiated
in a headless container without libGL).
"""

from __future__ import annotations

import time
from datetime import timezone
from pathlib import Path

from desktop import forecast_viewmodels as fvm
from desktop.forecast_controller import ForecastUiState

UTC = timezone.utc
SAMPLE_CSV = "data/sample_1h.csv"
DESKTOP_APP = Path(__file__).resolve().parents[1] / "desktop" / "app.py"


def snapshot_payload(**overrides) -> dict:
    payload = {
        "instrument": "DEMO",
        "timeframe": "1h",
        "origin": "KRONOS_FORECAST",
        "note": "MODEL FORECAST — not observed market data",
        "generated_at": "2024-01-02T00:00:00+00:00",
        "model_id": "NeoQuasar/Kronos-small",
        "model_revision": "901c26c1",
        "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base",
        "tokenizer_revision": "0e011738",
        "device": "cpu",
        "backend": "stub",
        "lookback": 400,
        "pred_len": 120,
        "max_context": 512,
        "sample_paths": 5,
        "temperature": 1.0,
        "top_p": 0.9,
        "top_k": 0,
        "last_closed_timestamp": "2024-01-01T23:00:00+00:00",
        "last_actual_close": 100.0,
        "forecast_endpoint": "2024-01-06T23:00:00+00:00",
        "horizon_close": 105.0,
        "horizon_return": 0.05,
        "uncertainty": {
            "sample_count": 5,
            "p10_close": 98.0,
            "p50_close": 105.0,
            "p90_close": 112.0,
            "dispersion_close": 3.5,
            "interval_width_close": 14.0,
            "interval_width_pct": 0.133,
            "supported": True,
            "calibrated": False,
        },
        "latency_ms": 1234.5,
        "input_hash": "a" * 64,
        "cache_key": "b" * 64,
        "provenance": {
            "source": "replay:data/sample_1h.csv",
            "provider": "replay",
            "bars": 400,
            "first_timestamp": "2023-12-16T00:00:00+00:00",
            "last_timestamp": "2024-01-01T23:00:00+00:00",
        },
        "data_quality": {"state": "OK", "errors": [], "warnings": [], "bars": 400},
        "candles": [
            {
                "timestamp": "2024-01-02T00:00:00+00:00",
                "forecast_open": 100.1,
                "forecast_high": 100.6,
                "forecast_low": 99.7,
                "forecast_close": 100.4,
                "forecast_volume": 1200.0,
                "origin": "KRONOS_FORECAST",
            }
        ],
    }
    payload.update(overrides)
    return payload


def status_payload(**overrides) -> dict:
    payload = {
        "state": "READY",
        "message": "NeoQuasar/Kronos-small loaded on cpu",
        "backend": "upstream",
        "model_id": "NeoQuasar/Kronos-small",
        "model_revision": "901c26c1",
        "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base",
        "tokenizer_revision": "0e011738",
        "device": "cpu",
        "torch_version": "2.14.0",
        "kronos_source": "/opt/Kronos/model/kronos.py",
        "cache_dir": "/home/tester/.cache/AXIOM/models/kronos",
        "loaded_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-02T00:00:00+00:00",
        "last_error": "",
        "last_inference_ms": 1234.5,
        "inferences": 7,
        "pid": 4242,
    }
    payload.update(overrides)
    return payload


class TestPanelLabels:
    def test_every_required_field_is_present(self):
        rows = dict(fvm.forecast_panel_rows(status_payload(), snapshot_payload()))
        for label in (
            "Status", "Model", "Model revision", "Tokenizer", "Tokenizer revision",
            "Device", "Instrument", "Timeframe", "Lookback", "Prediction horizon",
            "Last actual close (ACTUAL)", "Forecast endpoint (MODEL)",
            "Forecast horizon return (MODEL)", "Inference latency", "Updated",
            "Input data source", "Data quality (input)",
        ):
            assert label in rows, f"missing FORECAST panel field: {label}"

    def test_predicted_values_are_never_labelled_as_actual(self):
        rows = dict(fvm.forecast_panel_rows(status_payload(), snapshot_payload()))
        assert rows["Last actual close (ACTUAL)"].startswith("100.0")
        assert rows["Forecast close at horizon (MODEL)"].startswith("105.0")
        forbidden = {"Close", "Volume", "Open", "High", "Low"}
        assert not (forbidden & set(rows)), "unlabelled price/volume field in the panel"
        assert rows["Provenance"] == fvm.FORECAST_BANNER

    def test_dispersion_is_labelled_as_uncalibrated(self):
        rows = dict(fvm.forecast_panel_rows(status_payload(), snapshot_payload()))
        assert rows["Uncertainty meaning"] == (
            "sample dispersion (not calibrated probability)")
        assert "Forecast close P10 (MODEL)" in rows
        assert "Forecast close P90 (MODEL)" in rows

    def test_uncertainty_is_hidden_when_unsupported(self):
        payload = snapshot_payload(sample_paths=1, uncertainty=None)
        rows = dict(fvm.forecast_panel_rows(status_payload(), payload))
        assert "Forecast close P10 (MODEL)" not in rows
        assert "not available" in rows["Uncertainty"]

    def test_status_labels_are_human_readable(self):
        assert fvm.status_label("INSUFFICIENT_HISTORY").startswith("INSUFFICIENT_HISTORY")
        assert fvm.status_label("OFFLINE").startswith("OFFLINE")
        assert fvm.status_label(None) == "UNKNOWN"
        for state in ("DISABLED", "INITIALIZING", "DOWNLOADING", "LOADING", "READY",
                      "INFERENCING", "STALE", "OFFLINE", "ERROR",
                      "INSUFFICIENT_HISTORY", "DATA_ERROR"):
            assert state in fvm.STATE_LABELS

    def test_data_quality_label_shows_the_reason(self):
        assert fvm.data_quality_label({"state": "OK", "bars": 400}) == "OK (400 closed bars)"
        label = fvm.data_quality_label(
            {"state": "DATA_ERROR", "errors": ["row 3: non-finite OHLCV"]})
        assert label.startswith("DATA_ERROR")
        assert "non-finite" in label

    def test_no_snapshot_still_renders_a_panel(self):
        rows = dict(fvm.forecast_panel_rows(status_payload(state="LOADING"), None))
        assert rows["Status"].startswith("LOADING")
        assert rows["Forecast"] == "none yet"

    def test_last_error_is_surfaced(self):
        rows = dict(fvm.forecast_panel_rows(
            status_payload(state="ERROR", last_error="CUDA out of memory"), None))
        assert rows["Last error"] == "CUDA out of memory"


class TestOverlayAndTables:
    def test_overlay_keys_are_explicit(self):
        overlay = fvm.overlay_series({
            "actual_close": [("2024-01-01T00:00:00+00:00", 100.0)],
            "forecast_close": [("2024-01-02T00:00:00+00:00", 101.0)],
            "forecast_close_p10": [("2024-01-02T00:00:00+00:00", 99.0)],
            "forecast_close_p90": [("2024-01-02T00:00:00+00:00", 103.0)],
        })
        assert list(overlay) == ["ACTUAL close", "MODEL FORECAST close",
                                 "MODEL FORECAST P10", "MODEL FORECAST P90"]
        assert overlay["ACTUAL close"][0][1] == 100.0
        assert overlay["MODEL FORECAST close"][0][1] == 101.0

    def test_overlay_is_empty_without_a_forecast(self):
        overlay = fvm.overlay_series(None)
        assert all(values == [] for values in overlay.values())

    def test_legend_states_the_distinction(self):
        assert "ACTUAL" in fvm.overlay_legend()
        assert "MODEL FORECAST" in fvm.overlay_legend()

    def test_series_table_columns(self):
        rows = fvm.forecast_table_rows([snapshot_payload()])
        assert rows[0][3] == "last actual close (ACTUAL)"
        assert rows[0][4] == "forecast close (MODEL)"
        assert rows[1][0] == "DEMO"
        assert rows[1][5] == "5.00%"

    def test_forecast_candle_rows_are_labelled(self):
        rows = fvm.forecast_candle_rows(snapshot_payload())
        assert rows[0] == ["timestamp", "forecast open", "forecast high",
                           "forecast low", "forecast close", "forecast volume"]
        assert rows[1][4].startswith("100.4")


class TestController:
    def test_disabled_kronos_still_renders(self):
        controller = ForecastUiState().bootstrap({"AXIOM_KRONOS_ENABLED": "false"})
        assert controller.available is False
        rows = dict(controller.panel_rows())
        assert rows["Status"].startswith("DISABLED")
        assert "the terminal keeps working" in rows["AXIOM without Kronos"]
        assert controller.overlay()["MODEL FORECAST close"] == []
        assert controller.table_rows()[0][0] == "instrument"

    def test_missing_axiom_stack_is_not_fatal(self):
        controller = ForecastUiState()
        controller.error = "ImportError: no module named axiom"
        rows = dict(controller.panel_rows())
        assert "the terminal keeps working" in rows["AXIOM without Kronos"]
        assert "ImportError" in rows["Error"]

    def test_end_to_end_with_the_stub_backend(self):
        controller = ForecastUiState().bootstrap({
            "AXIOM_KRONOS_ENABLED": "true",
            "AXIOM_KRONOS_BACKEND": "stub",
            "AXIOM_KRONOS_MODE": "in-process",
            "AXIOM_KRONOS_LOOKBACK": "120",
            "AXIOM_KRONOS_PRED_LEN": "8",
            "AXIOM_KRONOS_MIN_HISTORY": "32",
            "AXIOM_KRONOS_SAMPLE_PATHS": "3",
            "AXIOM_MARKET_DATA_SOURCE": "replay",
            "AXIOM_MARKET_DATA_CSV": SAMPLE_CSV,
            "AXIOM_MARKET_DATA_TIMEFRAME": "1h",
            "AXIOM_FORECAST_SERIES": "DEMO:1h",
        })
        try:
            assert controller.available
            assert controller.series_labels() == ["DEMO · 1h"]
            deadline = time.perf_counter() + 20
            snapshot = None
            while snapshot is None and time.perf_counter() < deadline:
                controller.refresh()
                snapshot = controller.snapshot_dict()
                time.sleep(0.05)
            assert snapshot is not None, "no forecast reached the panel"

            rows = dict(controller.panel_rows())
            assert rows["Status"] == "READY"
            assert rows["Instrument"] == "DEMO"
            assert rows["Timeframe"] == "1h"
            assert rows["Sample paths"] == "3"
            assert rows["Data quality (input)"].startswith("OK")
            assert "MODEL FORECAST" in rows["Provenance"]

            actual, predicted = controller.chart_series()
            assert actual and predicted
            assert len(predicted) == 8
            overlay = controller.overlay()
            assert overlay["ACTUAL close"]
            assert len(overlay["MODEL FORECAST close"]) == 8
            assert len(overlay["MODEL FORECAST P10"]) == 8

            candle_rows = controller.forecast_candles(limit=3)
            assert candle_rows[0][4] == "forecast close"
            assert len(candle_rows) == 4  # header + 3 bars

            table = controller.table_rows()
            assert table[1][0] == "DEMO"
            assert controller.banner() == fvm.FORECAST_BANNER
        finally:
            controller.shutdown()

    def test_refresh_never_blocks_the_caller(self):
        controller = ForecastUiState().bootstrap({
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
            for _ in range(20):
                started = time.perf_counter()
                controller.refresh()
                assert (time.perf_counter() - started) < 0.1
                time.sleep(0.01)
        finally:
            controller.shutdown()


class TestDesktopStartup:
    """Startup smoke tests that need no display and no Qt libraries."""

    def test_missing_pyside6_exits_with_a_fix_hint(self, monkeypatch, capsys):
        """Without Qt the desktop app must print a fix and exit, not traceback.

        A ``None`` entry in ``sys.modules`` makes the deferred from-import
        raise ``ImportError`` exactly as an absent Qt install would, so this is
        deterministic whether or not PySide6 is importable on the host.
        """
        import sys

        from desktop import app as desktop_app

        monkeypatch.setitem(sys.modules, "PySide6.QtCore", None)
        assert desktop_app.main([]) == 2
        out = capsys.readouterr().out
        assert "PySide6 is required" in out
        assert "requirements-desktop.txt" in out

    def test_the_windows_launchers_reference_files_that_exist(self):
        """Guards against the .bat launchers drifting from the repo layout."""
        root = DESKTOP_APP.parents[1]
        for bat in ("desktop/run_windows.bat", "desktop/run_kronos_windows.bat"):
            text = (root / bat).read_text(encoding="utf-8")
            assert "python -m desktop" in text, f"{bat} no longer starts the app"
        for req in ("requirements-desktop.txt", "requirements-kronos.txt"):
            assert (root / req).exists(), f"{req} referenced by the launchers is gone"

    def test_the_kronos_launcher_pins_the_expected_python(self):
        root = DESKTOP_APP.parents[1]
        text = (root / "desktop" / "run_kronos_windows.bat").read_text(encoding="utf-8")
        assert "py -3.11" in text, "Kronos needs an explicit interpreter version"
        assert "AXIOM_KRONOS_SOURCE" in text


class TestDesktopWiring:
    def test_forecast_page_is_registered(self):
        source = DESKTOP_APP.read_text()
        start = source.index("PAGES = [")
        end = source.index("]", start)
        assert '"Forecast"' in source[start:end]

    def test_forecast_panel_and_overlay_are_built(self):
        source = DESKTOP_APP.read_text()
        for required in (
            "self.forecast = ForecastUiState()",
            "self.forecast.bootstrap()",
            "def _refresh_forecast(self)",
            'self.tables["Forecast"]',
            'self.charts["forecast_overlay"]',
            'self.charts["markets_forecast"]',
            "self.forecast.shutdown()",
        ):
            assert required in source, f"desktop wiring missing: {required}"

    def test_gui_refresh_is_non_blocking_by_construction(self):
        """The UI only queues work and drains results — no inference on the GUI
        thread, and no blocking manager call."""
        source = DESKTOP_APP.read_text()
        assert "self.forecast.refresh()" in source
        assert ".forecast(" not in source, "the GUI must not call the model directly"
        assert "manager.load(" not in source
