from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings


def client(tmp_path):
    settings = replace(Settings(), api_token="local-api-test", database_url=f"sqlite:///{tmp_path}/app.db")
    return TestClient(create_app(settings, data_dir=tmp_path))


def test_autonomy_page_status_and_manual_are_local_and_credentialed(tmp_path):
    with client(tmp_path) as web:
        assert web.get("/api/autonomy/status").status_code == 401
        headers = {"Authorization": "Bearer local-api-test"}
        status = web.get("/api/autonomy/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["automatic_arm_or_activate"] is False
        assert status.json()["profitability_claim"] == "none"
        page = web.get("/autonomy")
        assert page.status_code == 200
        assert "PAPER" in page.text and "UPSTOX_LIVE" in page.text
        assert "/static/autonomy/autonomy.js" in page.text
        assert "/static/vendor/lightweight-charts/" in page.text
        assert "https://" not in page.text
        manual = web.get("/autonomy/manual")
        assert manual.status_code == 200
        assert "not a live-readiness certificate" in manual.text
        script = web.get("/static/autonomy/autonomy.js")
        assert script.status_code == 200
        assert "fetch('/api/autonomy'" in script.text
        assert "localhost" not in script.text and "127.0.0.1" not in script.text


def test_kronos_setup_routes_are_authenticated_fixed_actions(tmp_path, monkeypatch):
    with client(tmp_path) as web:
        assert web.get("/api/autonomy/kronos").status_code == 401
        assert web.post("/api/autonomy/kronos/python", json={"executable": "python.exe"}).status_code == 401
        headers = {"Authorization": "Bearer local-api-test"}
        response = web.get("/api/autonomy/kronos", headers=headers)
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["install_plan"]["model"]["repository"] == "NeoQuasar/Kronos-small"
        assert view["install_plan"]["manifest_file_sha256"] == (
            "379ffadfb56911c7c85048e159286457008fcff684b2647b4ea847dc479785aa"
        )
        assert view["install_plan"]["runtime"]["packages"][0]["name"] == "torch"
        assert view["install_plan"]["runtime"]["cuda_stack"] is False
        assert view["execution_effect"].startswith("NONE")
        assert str(tmp_path) not in response.text  # no per-user paths or Python command leak
        service = web.app.state.autonomy
        calls = []
        monkeypatch.setattr(service, "kronos_install", lambda: calls.append("install") or {"fixed": True})
        monkeypatch.setattr(service, "kronos_self_test", lambda: calls.append("self-test") or {"fixed": True})
        monkeypatch.setattr(service, "kronos_cancel", lambda: calls.append("cancel") or {"fixed": True})
        selected = []
        monkeypatch.setattr(
            service, "kronos_python",
            lambda executable: selected.append(executable) or {"fixed": True},
        )
        python_path = r"C:\Users\operator\AppData\Local\Programs\Python\Python311\python.exe"
        result = web.post(
            "/api/autonomy/kronos/python", headers=headers, json={"executable": python_path},
        )
        assert result.status_code == 200 and result.json() == {"fixed": True}
        assert selected == [python_path] and python_path not in result.text
        rejected = web.post("/api/autonomy/kronos/python", headers=headers, json={
            "executable": python_path, "arguments": ["-c", "must-not-run"],
        })
        assert rejected.status_code == 422 and selected == [python_path]
        for action in ("install", "self-test"):
            result = web.post(f"/api/autonomy/kronos/{action}", headers=headers, json={
                "url": "https://evil.example/weights", "revision": "latest",
                "package": "torch", "credentials": "must-not-be-echoed",
            })
            expected = 202
            assert result.status_code == expected and result.json() == {"fixed": True}
            assert "evil.example" not in result.text and "must-not-be-echoed" not in result.text
        assert web.post("/api/autonomy/kronos/cancel", headers=headers).json() == {"fixed": True}
        assert calls == ["install", "self-test", "cancel"]


def test_credential_fields_are_rejected_and_never_echoed(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        secret = "not-a-real-token-but-must-not-be-echoed"
        response = web.post("/api/autonomy/sessions", headers=headers, json={
            "mode": "PAPER", "instrument_keys": ["NSE_EQ|INE000000000"],
            "access_token": secret,
        })
        assert response.status_code == 422
        assert secret not in response.text
        assert response.json()["detail"][0]["loc"][-1] == "access_token"


def test_no_generic_order_or_credential_api_is_exposed(tmp_path):
    with client(tmp_path) as web:
        schema = web.get("/openapi.json").json()
        paths = list(schema["paths"])
        assert "/api/autonomy/sessions/{session_id}/activate" in paths
        assert "/api/autonomy/sessions/{session_id}/arm" in paths
        assert "/api/autonomy/sessions/{session_id}/reconcile" in paths
        assert not any("place-order" in path or "token" in path.rsplit("/", 1)[-1]
                       for path in paths if path.startswith("/api/autonomy"))
        health = web.get("/health").json()
        assert health["mode"] == "PAPER" and health["live_trading"] is False
        assert health["autonomy"]["live_active"] is False


def test_live_scan_route_cannot_reach_execution_or_broker_mutation(tmp_path, monkeypatch):
    from tests.test_autonomy_service import activate, make_service

    live_dir = tmp_path / "live"
    live_dir.mkdir()
    service, store, feed, clock, session_id = make_service(live_dir, live=True)
    activate(service, session_id)
    runtime = service._runtime(session_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("Scan route reached an execution/broker mutation")

    monkeypatch.setattr(service, "execute_once", forbidden)
    for name in ("on_bar", "margin_for", "place_protected_entry", "modify_stop", "cancel_gtt"):
        monkeypatch.setattr(runtime.executor, name, forbidden)
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    with client(app_dir) as web:
        web.app.state.autonomy = service
        response = web.post(
            f"/api/autonomy/sessions/{session_id}/scan",
            headers={"Authorization": "Bearer local-api-test"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "ACTIVE"
        assert body["last_scan_kind"] == "READ_ONLY"
        assert body["scan_endpoint_effect"] == "READ_ONLY_NO_ORDER_MUTATIONS"
        assert body["intents"] == [] and runtime.executor.gtt == {}
        schema = web.get("/openapi.json").json()
        assert not any(path.endswith("/execute") or path.endswith("/execute-once")
                       for path in schema["paths"])
    store.close()


def test_incomplete_reconcile_returns_409_and_preserves_unresolved_intent(tmp_path, monkeypatch):
    from tests.test_autonomy_service import activate, make_service

    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    service, store, feed, clock, session_id = make_service(paper_dir)
    activate(service, session_id)
    service.execute_once(session_id)
    runtime = service._runtime(session_id)
    monkeypatch.setattr(runtime.executor, "get_gtt", lambda identifier: None)
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    with client(app_dir) as web:
        web.app.state.autonomy = service
        response = web.post(
            f"/api/autonomy/sessions/{session_id}/reconcile",
            headers={"Authorization": "Bearer local-api-test"},
        )
        assert response.status_code == 409, response.text
        assert "incomplete" in response.json()["detail"]
        assert service.public(session_id)["state"] == "HALTED"
        assert store.intents(session_id)[0]["state"] == "UNRESOLVED"
        arm = web.post(
            f"/api/autonomy/sessions/{session_id}/arm",
            headers={"Authorization": "Bearer local-api-test"},
        )
        assert arm.status_code == 409
    store.close()


def test_browser_recovery_route_revokes_active_approval_without_cancel(tmp_path):
    from tests.test_autonomy_service import activate, make_service

    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    service, store, feed, clock, session_id = make_service(paper_dir)
    activate(service, session_id)
    service.execute_once(session_id)
    runtime = service._runtime(session_id)
    before = runtime.executor.gtt
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    with client(app_dir) as web:
        web.app.state.autonomy = service
        response = web.post(
            f"/api/autonomy/sessions/{session_id}/recover-view",
            headers={"Authorization": "Bearer local-api-test"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "HALTED" and body["recovered"] is True
        assert body["authorized_day"] == "" and body["risk"] is None
        assert body["stop_requested"] is True and body["orders"] and body["intents"]
        assert runtime.executor.gtt == before
        assert all(rule["status"] != "CANCELLED"
                   for record in runtime.executor.gtt.values() for rule in record["rules"])
    store.close()
