"""V2.1: desktop logic (Qt-free parts) — view models + API client contract."""

import json
from urllib.error import URLError

from desktop import viewmodels as vm
from desktop.api_client import ArenaApiClient


class TestViewModels:
    def test_overview_rows_show_mode_and_pnl(self):
        rows = dict(vm.overview_rows({
            "mode_display": "UPSTOX_SANDBOX · LIVE DISABLED",
            "emergency_stop": True, "equity": 99637.58,
            "capital_initial": 100000.0, "drawdown": 0.012,
            "latest_snapshot": {"realized": -362.42, "unrealized": 0.0}}))
        assert rows["Execution mode"] == "UPSTOX_SANDBOX · LIVE DISABLED"
        assert rows["Emergency stop"] == "ACTIVE"
        assert rows["Total P&L"].startswith("-362.42")
        assert rows["Return %"] == "-0.36%"

    def test_null_safe_money_and_pct(self):
        assert vm.money(None) == "—"
        assert vm.pct(None) == "—"
        assert vm.money("not-a-number") == "not-a-number"

    def test_trade_rows_sandbox_labeled(self):
        rows = vm.trade_rows({"fills": [{"ts": "2026-09-25T05:00:00+00:00",
                                         "side": "SELL", "quantity": 3,
                                         "price": 101.5, "symbol": "RELIANCE"}]})
        assert rows[0] == ["time", "instrument", "action", "qty", "price", "mode"]
        assert rows[1][3] == "3.00" and rows[1][5] == "UPSTOX_SANDBOX"

    def test_market_data_rows_calendar_completeness(self):
        rows = dict(vm.market_data_rows({
            "universe": {"total": 79346, "state_active": 79346,
                         "state_removed": 0, "by_exchange": {"NSE": 79346}},
            "coverage": [{"instrument_key": "K", "unit": "days", "interval": 1,
                          "records": 90, "completeness": -1.0}]}))
        assert "n/a (calendar)" in rows["K days/1"]

    def test_experiment_rows_parse_recorded_results(self):
        rows = vm.experiment_rows({"experiments": [
            {"name": "g", "status": "complete",
             "result": json.dumps({"candidates": 3, "promoted": 1})}]})
        assert rows[1] == ["g", "complete", "3", "1"]


class TestApiClientContract:
    def test_client_sends_bearer_and_paths(self):
        captured = {}

        class FakeResponse:
            def __init__(self, payload):
                self._payload = json.dumps(payload).encode()

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            captured["method"] = request.get_method()
            return FakeResponse({"ok": True})

        import urllib.request

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            client = ArenaApiClient("http://127.0.0.1:8000/", "secret-token")
            assert client.overview()["ok"] is True
            assert captured["url"] == "http://127.0.0.1:8000/api/v2/overview"
            assert captured["auth"] == "Bearer secret-token"
            client.emergency_stop()
            assert captured["method"] == "POST"
            assert captured["url"].endswith("/engine/stop")
        finally:
            urllib.request.urlopen = original

    def test_client_error_is_surfacable(self):
        import urllib.request

        def failing(request, timeout=None):
            raise URLError("connection refused")

        original = urllib.request.urlopen
        urllib.request.urlopen = failing
        try:
            client = ArenaApiClient("http://127.0.0.1:8000", "t")
            try:
                client.risk()
                raise AssertionError("expected URLError")
            except URLError:
                pass
        finally:
            urllib.request.urlopen = original
