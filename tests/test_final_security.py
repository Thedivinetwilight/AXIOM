"""FINAL: security self-scan + fail-closed configuration guarantees."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class TestSecurityScan:
    def test_first_party_code_has_no_hardcoded_secrets(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "security_scan.py"), "--json"],
            capture_output=True, text=True, timeout=120)
        import json

        report = json.loads(result.stdout)
        assert report["findings"] == [], report["findings"]
        assert all(report["hygiene"].values()), report["hygiene"]

    def test_no_env_file_is_packaged_or_committed(self):
        gitignore = (ROOT / ".gitignore").read_text()
        assert ".env" in gitignore.split()
        assert not (ROOT / ".env").exists(), \
            "no real .env may exist in the repository tree"


class TestFailClosed:
    def test_live_mode_is_rejected(self):
        from app.config.settings import Settings

        with pytest.raises(ValueError):
            Settings(**{"mode": "paper", "live_trading": True})

    def test_broker_live_is_rejected(self):
        from app.config.settings import Settings

        with pytest.raises(ValueError):
            Settings(**{"broker": "LIVE"})

    def test_unknown_broker_is_rejected(self):
        from app.config.settings import Settings

        with pytest.raises(ValueError):
            Settings(**{"broker": "SOMETHING_ELSE"})

    def test_sandbox_requires_token(self):
        from app.config.settings import Settings

        with pytest.raises(ValueError):
            Settings(**{"broker": "UPSTOX_SANDBOX",
                        "upstox_sandbox_access_token": ""})

    def test_defaults_are_paper_and_local(self):
        from app.config.settings import Settings

        settings = Settings()
        assert settings.mode.upper() == "PAPER"
        assert settings.live_trading is False
        assert settings.broker.upper() == "LOCAL_PAPER"
