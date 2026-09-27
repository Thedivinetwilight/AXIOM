"""V1: CLI safety + live boundary. LIVE must be unreachable in every path."""

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
