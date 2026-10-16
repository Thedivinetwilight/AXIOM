"""V1: CLI safety + live boundary. LIVE must be unreachable in every path."""

import argparse
import dataclasses
import os
import subprocess
import sys

from app.config.settings import Settings

import pytest


class TestSettingsLiveBoundary:
    def test_broker_live_rejected(self, monkeypatch):
        monkeypatch.setenv("BROKER", "LIVE")
        with pytest.raises(ValueError):
            Settings.from_env()

    def test_lowercase_live_rejected(self, monkeypatch):
        monkeypatch.setenv("BROKER", "live")
        with pytest.raises(ValueError):
            Settings.from_env()

    def test_sandbox_requires_token(self, monkeypatch):
        monkeypatch.setenv("BROKER", "UPSTOX_SANDBOX")
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)
        with pytest.raises(ValueError):
            Settings.from_env()

    def test_defaults_are_paper(self, monkeypatch):
        for key in ("BROKER", "LIVE_TRADING", "MODE", "DATA_SOURCE"):
            monkeypatch.delenv(key, raising=False)
        settings = Settings.from_env()
        assert settings.broker == "LOCAL_PAPER"
        assert settings.live_trading is False
        assert settings.mode == "PAPER"


class TestCliSafety:
    def _run(self, *args, env_extra=None):
        env = dict(os.environ)
        env.setdefault("DATA_SOURCE", "generic")
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-m", "app", *args], capture_output=True, text=True,
            env=env, timeout=120)

    def test_engine_refuses_without_sandbox_config(self):
        result = self._run("engine", env_extra={"DATA_SOURCE": "generic"})
        assert result.returncode == 2
        assert "UPSTOX_SANDBOX" in result.stdout

    def test_sandbox_test_without_token_exit_3(self, monkeypatch):
        env = {"DATA_SOURCE": "generic"}
        for key in list(os.environ):
            if key.startswith("UPSTOX_"):
                env[key] = ""
        result = self._run("sandbox-test", env_extra={
            "UPSTOX_SANDBOX_ACCESS_TOKEN": ""})
        assert result.returncode == 3
        assert "nothing attempted, nothing claimed" in result.stdout

    def test_engine_requires_upstox_data_source(self, monkeypatch):
        result = self._run("engine", env_extra={
            "BROKER": "UPSTOX_SANDBOX",
            "UPSTOX_SANDBOX_ACCESS_TOKEN": "sbx",
            "DATA_SOURCE": "generic"})
        assert result.returncode == 2
        assert "DATA_SOURCE=upstox" in result.stdout


class TestSandboxTestReadsTokenAfterSettingsLoad:
    """cmd_sandbox_test used to read os.getenv() BEFORE _load_settings().
    Settings.from_env() is what calls load_dotenv(), so a token supplied only
    via .env was invisible and the command reported it as unset. Settings must
    be loaded first, and the token read from Settings.

    The broker is replaced with a sentinel so no network call is ever made.
    """

    class _Sentinel(Exception):
        pass

    def _run_cmd(self, monkeypatch, token):
        import app.__main__ as cli
        import app.brokers.sandbox as sbx
        from app.config.settings import Settings

        settings = dataclasses.replace(
            Settings(), upstox_sandbox_access_token=token)
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)

        def _no_network(*a, **k):
            raise self._Sentinel("reached broker construction")

        monkeypatch.setattr(sbx, "UpstoxSandboxBroker", _no_network)
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)

        args = argparse.Namespace(instrument="NSE_EQ|INE002A01018", place_order=False)
        with pytest.raises(self._Sentinel):
            cli.cmd_sandbox_test(args)

    def test_a_token_from_settings_reaches_the_broker(self, monkeypatch, capsys):
        """A .env-only token must be found - i.e. we get past the unset branch."""
        self._run_cmd(monkeypatch, "secret-from-dotenv")
        assert "not set" not in capsys.readouterr().out

    def test_a_genuinely_absent_token_still_exits_3(self, monkeypatch, capsys):
        import app.__main__ as cli
        from app.config.settings import Settings

        settings = dataclasses.replace(Settings(), upstox_sandbox_access_token="")
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)

        args = argparse.Namespace(instrument="NSE_EQ|INE002A01018", place_order=False)
        assert cli.cmd_sandbox_test(args) == 3
        assert "nothing attempted, nothing claimed" in capsys.readouterr().out

    def test_a_rejected_config_exits_2_without_any_request(self, monkeypatch, capsys):
        import app.__main__ as cli

        def _reject():
            raise ValueError("Only MODE=PAPER and LIVE_TRADING=false are permitted")

        monkeypatch.setattr(cli, "_load_settings", _reject)
        args = argparse.Namespace(instrument="NSE_EQ|INE002A01018", place_order=False)
        assert cli.cmd_sandbox_test(args) == 2
        assert "configuration rejected" in capsys.readouterr().out


class TestCliCanNeverPlaceALiveOrder:
    """FINAL-11/10 §60: the CLI's --place-order may only ever touch the
    sandbox. If the broker is (by any path) a LIVE one, the command must
    refuse and submit nothing — real-money orders exist only through the
    live execution gateway. This counts broker.submit calls to prove it.
    """

    def test_a_live_broker_is_refused_with_zero_submissions(self, monkeypatch,
                                                             capsys):
        import app.__main__ as cli
        import app.brokers.sandbox as sbx
        from app.config.settings import Settings

        submitted: list = []

        class FakeLiveBroker:
            LIVE = True
            MODE = "UPSTOX_LIVE"

            def __init__(self, *a, **k):
                pass

            def _request(self, *a, **k):
                return {}

            def submit(self, *a, **k):  # pragma: no cover - must never run
                submitted.append((a, k))
                raise AssertionError("a CLI flag must never place a live order")

        settings = dataclasses.replace(Settings(),
                                       upstox_sandbox_access_token="t")
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)
        monkeypatch.setattr(sbx, "UpstoxSandboxBroker", FakeLiveBroker)
        # force the probe to read "credential accepted" so we reach the
        # place-order branch — the refusal must still hold there.
        monkeypatch.setattr(sbx, "classify_history_probe",
                            lambda resp: {"credential_accepted": True,
                                          "http_status": 404,
                                          "class": "order_not_found",
                                          "codes": []})
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)

        args = argparse.Namespace(instrument="NSE_EQ|INE002A01018",
                                  place_order=True, quantity=1)
        assert cli.cmd_sandbox_test(args) == 2
        out = capsys.readouterr().out
        assert "sandbox-only" in out
        assert submitted == []          # nothing was ever sent to the broker


class TestLocalPaperNeverConstructsTheSandboxBroker:
    """The one-week paper test runs BROKER=LOCAL_PAPER with DATA_SOURCE=upstox.
    Upstox is then a MARKET DATA source only; no order-mutating broker may exist.
    """

    def _with_sentinel(self, monkeypatch):
        import app.brokers.sandbox as sbx
        hits = []

        def _sentinel(*a, **k):
            hits.append(k.get("base_url") or (a[1] if len(a) > 1 else "?"))
            raise AssertionError("UpstoxSandboxBroker must never be constructed")

        monkeypatch.setattr(sbx, "UpstoxSandboxBroker", _sentinel)
        return hits

    def test_engine_refuses_local_paper_before_any_broker_exists(self, monkeypatch, capsys):
        import app.__main__ as cli
        hits = self._with_sentinel(monkeypatch)
        settings = dataclasses.replace(
            Settings(), broker="LOCAL_PAPER", data_source="upstox")
        monkeypatch.setattr(cli, "_load_settings", lambda: settings)

        args = argparse.Namespace(instrument="NSE_EQ|INE002A01018", place_order=False)
        assert cli.cmd_engine(args) == 2
        assert hits == [], "sandbox broker was constructed in LOCAL_PAPER mode"
        assert "UPSTOX_SANDBOX" in capsys.readouterr().out

    def test_local_paper_with_upstox_data_is_a_valid_configuration(self, monkeypatch):
        """The intended one-week setup must not be rejected by Settings."""
        for k, v in {"BROKER": "LOCAL_PAPER", "DATA_SOURCE": "upstox",
                     "MODE": "PAPER", "LIVE_TRADING": "false",
                     "DEFAULT_TIMEFRAME": "1m",
                     "MARKET_DATA_TIMEFRAME": "1m"}.items():
            monkeypatch.setenv(k, v)
        s = Settings.from_env()
        assert s.broker == "LOCAL_PAPER"
        assert s.data_source == "upstox"
        assert s.live_trading is False

    def test_sandbox_broker_still_requires_its_explicit_mode(self, monkeypatch):
        """Switching to LOCAL_PAPER must not make the sandbox broker reachable."""
        monkeypatch.setenv("BROKER", "LOCAL_PAPER")
        monkeypatch.delenv("UPSTOX_SANDBOX_ACCESS_TOKEN", raising=False)
        assert Settings.from_env().broker == "LOCAL_PAPER"
        monkeypatch.setenv("BROKER", "UPSTOX_SANDBOX")
        with pytest.raises(ValueError):
            Settings.from_env()
