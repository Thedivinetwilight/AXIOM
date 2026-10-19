"""OAuth login must read the .env it writes the token into.

Regression tests for the packaged-build defect where `run_login_flow` imported
python-dotenv inside a `try/except ImportError: pass`. In a build without that
optional dependency a correctly configured .env was ignored and the operator
was told to "Set UPSTOX_CLIENT_ID ... first" — dead end for Upstox setup.
"""

from __future__ import annotations

import os
import sys

import pytest

from app.data import upstox_auth

KEYS = ("UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET", "UPSTOX_REDIRECT_URI")


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "UPSTOX_CLIENT_ID=from-file\n"
        "UPSTOX_CLIENT_SECRET=file-secret\n"
        "UPSTOX_REDIRECT_URI=https://127.0.0.1:8765/callback\n",
        encoding="utf-8",
    )
    return env_path


def _run(monkeypatch, env_path, captured):
    def fake_exchange(code, client_id, client_secret, redirect_uri, **kwargs):
        captured.update(code=code, client_id=client_id,
                        client_secret=client_secret, redirect_uri=redirect_uri)
        return "token-value"

    monkeypatch.setattr(upstox_auth, "exchange_code_for_token", fake_exchange)
    monkeypatch.setattr(upstox_auth, "store_token_in_env",
                        lambda token, path: "stored (test)")
    return upstox_auth.run_login_flow(
        env_path, capture=lambda: ("auth-code", None))


def test_file_values_are_used_when_dotenv_is_unavailable(
        isolated_env, monkeypatch, capsys):
    # A frozen build may not carry the optional python-dotenv package. The
    # old implementation swallowed that ImportError and then claimed the
    # .env values were missing.
    monkeypatch.setitem(sys.modules, "dotenv", None)
    captured: dict = {}
    assert _run(monkeypatch, isolated_env, captured) == "stored"
    assert captured["client_id"] == "from-file"
    assert captured["client_secret"] == "file-secret"
    assert captured["redirect_uri"] == "https://127.0.0.1:8765/callback"
    out = capsys.readouterr().out
    assert "client_id=from-file" in out
    # The secret and token must never be printed.
    assert "file-secret" not in out and "token-value" not in out


def test_exported_variable_wins_over_the_file(
        isolated_env, monkeypatch):
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "from-shell")
    captured: dict = {}
    _run(monkeypatch, isolated_env, captured)
    assert captured["client_id"] == "from-shell"


def test_process_environment_is_not_mutated(isolated_env, monkeypatch):
    _run(monkeypatch, isolated_env, {})
    # A later component must not silently inherit file-only credentials.
    assert "UPSTOX_CLIENT_SECRET" not in os.environ


def test_missing_configuration_names_the_file(tmp_path, monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(upstox_auth.UpstoxOAuthError) as exc:
        upstox_auth.run_login_flow(tmp_path / "absent.env",
                                   capture=lambda: ("code", None))
    assert "UPSTOX_CLIENT_ID" in str(exc.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_unreadable_file_is_reported_not_ignored(tmp_path, monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("UPSTOX_CLIENT_ID=x\n", encoding="utf-8")
    env_path.chmod(0o000)
    try:
        with pytest.raises(upstox_auth.UpstoxOAuthError) as exc:
            upstox_auth.run_login_flow(env_path, capture=lambda: ("code", None))
    finally:
        env_path.chmod(0o600)
    assert "Could not read" in str(exc.value)
