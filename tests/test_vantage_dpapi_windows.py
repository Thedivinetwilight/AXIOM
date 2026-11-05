"""Real Windows DPAPI tests with dummy values; no broker account is contacted."""

import os

import pytest

from app.vantage.secrets import DpapiSecretStore


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is available only on Windows")
def test_dpapi_round_trip_is_not_plaintext_and_delete_works(tmp_path):
    store = DpapiSecretStore(tmp_path)
    dummy = {
        "login": "123456", "password": "dummy-windows-test-only",
        "server": "VantageMarkets-Demo", "terminal_path": "",
    }
    store.put("VANTAGE_DEMO", dummy)
    path = tmp_path / "vantage_demo.dpapi"
    encrypted = path.read_bytes()
    assert encrypted
    assert dummy["password"].encode() not in encrypted
    assert dummy["login"].encode() not in encrypted
    assert store.get("VANTAGE_DEMO") == dummy
    store.delete("VANTAGE_DEMO")
    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is available only on Windows")
def test_dpapi_replace_recovers_from_interrupted_temporary_file(tmp_path):
    store = DpapiSecretStore(tmp_path)
    (tmp_path / "vantage_demo.tmp").write_bytes(b"interrupted dummy write")
    store.put("VANTAGE_DEMO", {
        "login": "123456", "password": "dummy-replacement-only",
        "server": "VantageMarkets-Demo", "terminal_path": "",
    })
    assert store.get("VANTAGE_DEMO")["server"] == "VantageMarkets-Demo"
