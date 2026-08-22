"""Settings + dashboard endpoints through the real ASGI app."""
import pytest

from app import runtime
from app.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, webui, worker
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
    # Isolate saved settings and tracked state so a POST or a seeded history
    # event here never leaks into another test.
    st = Store(str(tmp_path / "state.db"))
    monkeypatch.setattr(runtime, "store", st)
    monkeypatch.setattr(webui, "store", st)
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


def _store():
    """The isolated Store the `client` fixture wired into webui."""
    from app import webui
    return webui.store


def _seed_history():
    for h, name, cat, event in [
        ("h1", "Alpha Movie", "radarr", "added"),
        ("h2", "Beta Show", "sonarr", "added"),
        ("h3", "Alpha Movie", "radarr", "downloaded"),
        ("h4", "Gamma Movie", "", "error"),
    ]:
        _store().add_event(h, name, cat, event, detail="d", size=10)


def test_history_requires_auth(client):
    assert client.get("/ui/api/history").status_code == 403


def test_history_pages_and_reports_totals(client):
    _login(client)
    _seed_history()
    page1 = client.get("/ui/api/history?limit=2&offset=0").json()
    assert len(page1["events"]) == 2
    assert page1["total"] == 4 and page1["total_all"] == 4
    assert page1["offset"] == 0 and page1["limit"] == 2

    page2 = client.get("/ui/api/history?limit=2&offset=2").json()
    assert page2["offset"] == 2 and len(page2["events"]) == 2
    # Pages do not overlap.
    assert not {e["hash"] for e in page2["events"]} & {e["hash"] for e in page1["events"]}

    assert client.get("/ui/api/history?limit=2&offset=99").json()["events"] == []


def test_history_search_and_filters(client):
    _login(client)
    _seed_history()
    assert client.get("/ui/api/history?q=alpha").json()["total"] == 2
    assert client.get("/ui/api/history?events=added").json()["total"] == 2
    assert client.get("/ui/api/history?events=added,error").json()["total"] == 3
    assert client.get("/ui/api/history?categories=radarr").json()["total"] == 2

    # __none__ is how the UI asks for uncategorised in a comma-separated list.
    uncategorised = client.get("/ui/api/history?categories=__none__").json()
    assert uncategorised["total"] == 1
    assert uncategorised["events"][0]["name"] == "Gamma Movie"

    # total narrows with the filter; total_all stays the size of the whole log.
    filtered = client.get("/ui/api/history?q=alpha").json()
    assert filtered["total"] == 2 and filtered["total_all"] == 4


def test_history_facets_drive_the_filter_dropdowns(client):
    _login(client)
    _seed_history()
    facets = client.get("/ui/api/history").json()["facets"]
    assert set(facets["events"]) == {"added", "downloaded", "error"}
    assert "radarr" in facets["categories"]
    assert "" in facets["categories"]  # so the UI can offer "uncategorised"


def test_history_clamps_limit_and_offset(client):
    _login(client)
    _seed_history()
    assert client.get("/ui/api/history?limit=99999").json()["limit"] == 500
    assert client.get("/ui/api/history?limit=0").json()["limit"] == 1
    assert client.get("/ui/api/history?offset=-5").json()["offset"] == 0


def test_history_bad_sort_key_is_ignored(client):
    """The sort key is user input, so it must never reach SQL unchecked."""
    _login(client)
    _seed_history()
    injected = client.get("/ui/api/history?sort=id%3BDROP+TABLE+history").json()
    assert injected["total"] == 4
    assert client.get("/ui/api/history").json()["total"] == 4  # table still there


def test_lowering_history_retention_prunes_immediately(client):
    _login(client)
    for i in range(150):
        _store().add_event(f"r{i}", f"name{i}", "radarr", "added")
    assert _store().history_count() == 150  # default retention is 1000

    resp = client.post("/ui/api/settings", json={"history_retention": 100})
    assert resp.status_code == 200
    assert resp.json()["settings"]["history_retention"] == 100
    # add_event() only prunes every 100th insert, so the save has to trim itself.
    assert _store().history_count() == 100

    assert client.post("/ui/api/settings",
                       json={"history_retention": 50}).status_code == 400  # below minimum
