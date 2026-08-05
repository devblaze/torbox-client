"""Settings + dashboard endpoints through the real ASGI app."""
import pytest

from app import runtime
from app.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, worker
    from app.torbox_client import client as tb

    async def _noop():
        return

    async def _ok():
        return True

    monkeypatch.setattr(worker, "run", _noop)
    monkeypatch.setattr(worker, "housekeeping", _noop)
    monkeypatch.setattr(worker, "repair_paths", lambda: None)
    monkeypatch.setattr(worker, "resume_interrupted", lambda: None)
    monkeypatch.setattr(tb, "validate_key", _ok)
    # Isolate saved settings so a POST here never leaks into other tests.
    st = Store(str(tmp_path / "state.db"))
    monkeypatch.setattr(runtime, "store", st)
    runtime.load()
    with TestClient(main.app) as c:
        yield c
    monkeypatch.undo()
    runtime.load()


def _login(client):
    from app.config import settings
    resp = client.post("/api/v2/auth/login",
                       data={"username": settings.qbit_user, "password": settings.qbit_pass})
    assert resp.text == "Ok."


def test_settings_require_auth(client):
    assert client.get("/ui/api/settings").status_code == 403
    assert client.post("/ui/api/settings", json={}).status_code == 403
    assert client.post("/ui/api/notify-test").status_code == 403


def test_settings_roundtrip(client):
    _login(client)
    resp = client.get("/ui/api/settings")
    assert resp.status_code == 200
    assert resp.json()["settings"]["max_download_speed"] == 0

    resp = client.post("/ui/api/settings",
                       json={"max_download_speed": 3.5, "cloud_max_age_days": 30})
    assert resp.status_code == 200
    assert resp.json()["settings"]["max_download_speed"] == 3.5
    assert runtime.get("cloud_max_age_days") == 30


def test_settings_reject_bad_values(client):
    _login(client)
    assert client.post("/ui/api/settings",
                       json={"max_download_speed": -1}).status_code == 400
    assert client.post("/ui/api/settings",
                       json={"no_such_setting": 1}).status_code == 400
    assert client.post("/ui/api/settings", json=[1, 2]).status_code == 400
    assert runtime.get("max_download_speed") == 0  # nothing applied


def test_state_includes_subscription_field(client):
    _login(client)
    data = client.get("/ui/api/state").json()
    assert "subscription" in data  # None until the first housekeeping pass


def test_notify_test_reports_unconfigured(client):
    _login(client)
    data = client.post("/ui/api/notify-test").json()
    assert data["ok"] is False
    assert "not configured" in data["detail"]
