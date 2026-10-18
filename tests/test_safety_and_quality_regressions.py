"""Regression tests for confirmed safety properties and code-quality fixes.

These tests verify:
1. Safety configuration defaults (fail-closed paper mode).
2. Four-switch live gate (any single missing switch refuses to boot).
3. Host classification (live host cannot be used for sandbox, and vice-versa).
4. No credentials in .env.example.
5. No .env files tracked in git.
6. Unused imports/variables regression (ruff F401/F841 rules on touched files).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Safety defaults
# ---------------------------------------------------------------------------


class TestSafetyDefaults:
    """Every fresh install must boot in paper mode."""

    def test_default_mode_is_paper(self):
        from app.config.settings import Settings

        s = Settings()
        assert s.mode == "PAPER"
        assert s.live_trading is False
        assert s.broker == "LOCAL_PAPER"

    def test_from_env_without_env_file(self, monkeypatch):
        """Settings.from_env() with no .env present must still default to paper."""
        # Clear any env vars that might override
        for key in ("MODE", "LIVE_TRADING", "BROKER", "LIVE_CONFIRM"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("UPSTOX_LIVE_ACCESS_TOKEN", raising=False)
        from app.config.settings import Settings

        s = Settings.from_env()
        assert s.mode == "PAPER"
        assert s.live_trading is False
        assert s.broker == "LOCAL_PAPER"


# ---------------------------------------------------------------------------
# Four-switch live gate
# ---------------------------------------------------------------------------


class TestLiveGateFailClosed:
    """Any single missing switch must refuse to construct Settings."""

    BASE_ENV = {
        "MODE": "LIVE",
        "LIVE_TRADING": "true",
        "BROKER": "UPSTOX_LIVE",
        "LIVE_CONFIRM": "I-ACCEPT-REAL-MONEY-RISK",
        "UPSTOX_LIVE_ACCESS_TOKEN": "test-token",
        "MAX_POSITION_VALUE": "100000",
        "MAX_OPEN_POSITIONS": "5",
        "MAX_DAILY_LOSS": "5000",
        "MAX_TRADES_PER_DAY": "20",
    }

    def _make_settings(self, overrides: dict):
        from app.config.settings import Settings

        env = {**self.BASE_ENV, **overrides}
        # Need to also provide non-live settings that Settings validates
        env.setdefault("DATABASE_URL", "sqlite:///tmp_test.db")
        env.setdefault("DATA_SOURCE", "replay")
        env.setdefault("MARKET_CALENDAR", "continuous")
        return Settings(**{
            k.lower(): v for k, v in env.items()
            if k.lower() in {
                f.name for f in __import__("dataclasses").fields(Settings)
            }
        })

    def test_missing_mode_live_refused(self):
        with pytest.raises(ValueError, match="MODE=LIVE"):
            self._make_settings({"MODE": "PAPER"})

    def test_missing_live_trading_refused(self):
        with pytest.raises(ValueError, match="LIVE_TRADING=true"):
            self._make_settings({"LIVE_TRADING": "false"})

    def test_missing_live_confirm_refused(self):
        with pytest.raises(ValueError, match="LIVE_CONFIRM"):
            self._make_settings({"LIVE_CONFIRM": ""})

    def test_wrong_live_confirm_phrase_refused(self):
        with pytest.raises(ValueError, match="LIVE_CONFIRM"):
            self._make_settings({"LIVE_CONFIRM": "WRONG-PHRASE"})

    def test_missing_live_token_refused(self):
        with pytest.raises(ValueError, match="UPSTOX_LIVE_ACCESS_TOKEN"):
            self._make_settings({"UPSTOX_LIVE_ACCESS_TOKEN": ""})

    def test_zero_max_position_value_refused(self):
        with pytest.raises(ValueError, match="MAX_POSITION_VALUE"):
            self._make_settings({"MAX_POSITION_VALUE": "0"})

    def test_zero_max_open_positions_refused(self):
        with pytest.raises(ValueError, match="MAX_OPEN_POSITIONS"):
            self._make_settings({"MAX_OPEN_POSITIONS": "0"})

    def test_zero_max_daily_loss_refused(self):
        with pytest.raises(ValueError, match="MAX_DAILY_LOSS"):
            self._make_settings({"MAX_DAILY_LOSS": "0"})

    def test_zero_max_trades_per_day_refused(self):
        with pytest.raises(ValueError, match="MAX_TRADES_PER_DAY"):
            self._make_settings({"MAX_TRADES_PER_DAY": "0"})

    def test_broker_alias_live_refused(self):
        """BROKER=LIVE alone must always raise."""
        with pytest.raises(ValueError, match="BROKER=LIVE"):
            from app.config.settings import Settings
            Settings(broker="LIVE")


# ---------------------------------------------------------------------------
# Host classification
# ---------------------------------------------------------------------------


class TestHostClassification:
    """Live and sandbox hosts must never be interchangeable."""

    def test_live_host_not_in_sandbox_hosts(self):
        from app.config.hosts import LIVE_ORDER_HOSTS, SANDBOX_ORDER_HOSTS
        assert LIVE_ORDER_HOSTS.isdisjoint(SANDBOX_ORDER_HOSTS)

    def test_api_hft_is_live_only(self):
        from app.config.hosts import LIVE_ORDER_HOSTS, SANDBOX_ORDER_HOSTS
        assert "api-hft.upstox.com" in LIVE_ORDER_HOSTS
        assert "api-hft.upstox.com" not in SANDBOX_ORDER_HOSTS

    def test_api_sandbox_is_sandbox_only(self):
        from app.config.hosts import LIVE_ORDER_HOSTS, SANDBOX_ORDER_HOSTS
        assert "api-sandbox.upstox.com" in SANDBOX_ORDER_HOSTS
        assert "api-sandbox.upstox.com" not in LIVE_ORDER_HOSTS

    def test_sandbox_forbidden_includes_live_hosts(self):
        from app.config.hosts import FORBIDDEN_IN_SANDBOX, LIVE_ORDER_HOSTS
        assert LIVE_ORDER_HOSTS.issubset(FORBIDDEN_IN_SANDBOX)

    def test_live_forbidden_includes_sandbox_hosts(self):
        from app.config.hosts import FORBIDDEN_IN_LIVE, SANDBOX_ORDER_HOSTS
        assert SANDBOX_ORDER_HOSTS.issubset(FORBIDDEN_IN_LIVE)

    def test_https_required_for_broker_urls(self):
        from app.config.hosts import is_trusted_upstox_url, SANDBOX_ORDER_HOSTS
        assert not is_trusted_upstox_url("http://api-sandbox.upstox.com", SANDBOX_ORDER_HOSTS)
        assert is_trusted_upstox_url("https://api-sandbox.upstox.com", SANDBOX_ORDER_HOSTS)

    def test_no_userinfo_in_broker_urls(self):
        from app.config.hosts import is_trusted_upstox_url, SANDBOX_ORDER_HOSTS
        assert not is_trusted_upstox_url(
            "https://user:pass@api-sandbox.upstox.com", SANDBOX_ORDER_HOSTS)

    def test_no_query_params_in_broker_urls(self):
        from app.config.hosts import is_trusted_upstox_url, SANDBOX_ORDER_HOSTS
        assert not is_trusted_upstox_url(
            "https://api-sandbox.upstox.com?key=value", SANDBOX_ORDER_HOSTS)


# ---------------------------------------------------------------------------
# Credential protection
# ---------------------------------------------------------------------------


class TestCredentialProtection:
    """No real credentials in tracked files."""

    def test_env_example_has_no_real_credentials(self):
        env_example = ROOT / ".env.example"
        if not env_example.exists():
            pytest.skip(".env.example not found")
        content = env_example.read_text(encoding="utf-8")
        # Credential fields that must be empty in the example.
        # Only fields with ACCESS_TOKEN, SECRET, PASSWORD, or bare TOKEN
        # (not compound names like TOKEN_MAX_AGE_HOURS).
        credential_keys = {
            "API_TOKEN", "MARKET_DATA_TOKEN", "MARKET_DATA_ACCESS_TOKEN",
            "UPSTOX_ACCESS_TOKEN", "UPSTOX_SANDOX_ACCESS_TOKEN",
            "UPSTOX_SANDBOX_ACCESS_TOKEN", "UPSTOX_LIVE_ACCESS_TOKEN",
            "UPSTOX_CLIENT_SECRET", "LIVE_CONFIRM",
        }
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip().upper()
            value = value.strip()
            if key in credential_keys:
                assert value == "", f"{key} must be empty in .env.example, got {value!r}"

    def test_no_env_tracked_in_git(self):
        """No .env file should be tracked in git."""
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        tracked = result.stdout.strip().splitlines()
        env_tracked = [f for f in tracked if f.endswith(".env") and ".example" not in f]
        assert env_tracked == [], f"Tracked .env files: {env_tracked}"


# ---------------------------------------------------------------------------
# Code quality regressions (ruff F401/F841)
# ---------------------------------------------------------------------------


class TestCodeQualityRegressions:
    """Verify that specific unused-import/variable bugs remain fixed."""

    def test_no_unused_sys_in_instance(self):
        content = (ROOT / "app" / "instance.py").read_text(encoding="utf-8")
        lines = content.splitlines()
        import_lines = [l for l in lines if l.strip() == "import sys"]
        assert import_lines == [], "Unused 'import sys' should have been removed from instance.py"

    def test_no_unused_field_in_registry(self):
        content = (ROOT / "app" / "markets" / "registry.py").read_text(encoding="utf-8")
        assert "field" not in content.split("import")[1].split("\n")[0] or \
               "from dataclasses import dataclass, field" not in content, \
               "Unused 'field' import should have been removed from registry.py"

    def test_no_unused_callable_in_changes(self):
        content = (ROOT / "app" / "state" / "changes.py").read_text(encoding="utf-8")
        assert "Callable" not in content.split("from typing import")[1].split("\n")[0], \
               "Unused 'Callable' import should have been removed from changes.py"

    def test_no_unused_deque_in_latency(self):
        content = (ROOT / "app" / "system" / "latency.py").read_text(encoding="utf-8")
        assert "from collections import deque" not in content, \
               "Unused 'deque' import should have been removed from latency.py"

    def test_no_unused_time_in_resources(self):
        content = (ROOT / "app" / "system" / "resources.py").read_text(encoding="utf-8")
        assert "import time" not in content, \
               "Unused 'import time' should have been removed from resources.py"

    def test_no_unused_exc_in_upstox_auth(self):
        content = (ROOT / "app" / "data" / "upstox_auth.py").read_text(encoding="utf-8")
        assert "ConnectError as exc" not in content, \
               "Unused 'exc' variable should have been removed from upstox_auth.py"