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
