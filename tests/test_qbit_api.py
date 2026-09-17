import os

import pytest

from app import qbit_api


# --------------------------------------------------------------------------- #
# credential + session helpers
# --------------------------------------------------------------------------- #
def test_credentials_ok():
    from app.config import settings
    assert qbit_api._credentials_ok(settings.qbit_user, settings.qbit_pass) is True
    assert qbit_api._credentials_ok(settings.qbit_user, "wrong") is False
    assert qbit_api._credentials_ok("wrong", settings.qbit_pass) is False


def test_login_throttle(monkeypatch):
    qbit_api._failed_logins.clear()
    ip = "10.0.0.9"
    assert qbit_api._login_blocked(ip) is False
    for _ in range(qbit_api._LOGIN_MAX_FAILS):
        qbit_api._record_login_failure(ip)
    assert qbit_api._login_blocked(ip) is True
    # A different IP is unaffected.
    assert qbit_api._login_blocked("10.0.0.10") is False


def test_login_throttle_window_resets(monkeypatch):
    qbit_api._failed_logins.clear()
    ip = "10.0.0.11"
    for _ in range(qbit_api._LOGIN_MAX_FAILS):
        qbit_api._record_login_failure(ip)
    # Force the window to look expired.
    count, _start = qbit_api._failed_logins[ip]
    qbit_api._failed_logins[ip] = (count, 0.0)
    assert qbit_api._login_blocked(ip) is False


# --------------------------------------------------------------------------- #
# SSRF host classification
#
# getaddrinfo is mocked so these are hermetic — a real resolver (e.g. a CI
# runner using DNS64/NAT64) can return extra records and make network-dependent
# assertions flaky.
# --------------------------------------------------------------------------- #
import socket


def _fake_getaddrinfo(*ips):
    def _gai(host, port, *a, **k):
        out = []
        for ip in ips:
            if ":" in ip:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)))
        return out
    return _gai


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "192.168.1.5",
                                "169.254.169.254", "::1", "224.0.0.1", "0.0.0.0"])
def test_host_is_public_rejects_internal(monkeypatch, ip):
    monkeypatch.setattr(qbit_api.socket, "getaddrinfo", _fake_getaddrinfo(ip))
    assert qbit_api._host_is_public("evil.example") is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1"])
def test_host_is_public_allows_public(monkeypatch, ip):
    monkeypatch.setattr(qbit_api.socket, "getaddrinfo", _fake_getaddrinfo(ip))
    assert qbit_api._host_is_public("indexer.example") is True


def test_host_is_public_rejects_when_any_record_is_internal(monkeypatch):
    # DNS-rebinding style: one public, one private -> must be rejected.
    monkeypatch.setattr(qbit_api.socket, "getaddrinfo", _fake_getaddrinfo("8.8.8.8", "10.0.0.1"))
    assert qbit_api._host_is_public("mixed.example") is False


def test_host_is_public_unresolvable_is_false(monkeypatch):
    def _raise(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(qbit_api.socket, "getaddrinfo", _raise)
    assert qbit_api._host_is_public("nonexistent.invalid") is False


# --------------------------------------------------------------------------- #
# qBittorrent state mapping
# --------------------------------------------------------------------------- #
def test_qbit_state_mapping():
    from app.store import (STATE_CLOUD, STATE_COMPLETED, STATE_DOWNLOADING,
                           STATE_ERROR, STATE_QUEUED, Torrent)
    mk = lambda **k: Torrent(hash="a" * 40, name="x", **k)
    assert qbit_api._qbit_state(mk(state=STATE_ERROR)) == "error"
    assert qbit_api._qbit_state(mk(state=STATE_COMPLETED)) == "pausedUP"
    assert qbit_api._qbit_state(mk(state=STATE_DOWNLOADING)) == "downloading"
    assert qbit_api._qbit_state(mk(state=STATE_QUEUED)) == "metaDL"
    assert qbit_api._qbit_state(mk(state=STATE_CLOUD, dlspeed=0)) == "stalledDL"
    assert qbit_api._qbit_state(mk(state=STATE_CLOUD, dlspeed=5)) == "downloading"


def test_cloud_finished_reports_queued_not_stalled():
    """TorBox is done; we are waiting for a local pull slot. Sonarr renders
    stalledDL as "The download is stalled with no connections" — queuedDL is
    both accurate and free of the false alarm."""
    from app.store import STATE_CLOUD, Torrent
    mk = lambda **k: Torrent(hash="a" * 40, name="x", state=STATE_CLOUD, **k)
    assert qbit_api._qbit_state(mk(cloud_progress=1.0, dlspeed=0)) == "queuedDL"
    # Still genuinely downloading in the cloud with no peers -> still stalled.
    assert qbit_api._qbit_state(mk(cloud_progress=0.4, dlspeed=0)) == "stalledDL"


# --------------------------------------------------------------------------- #
# full login flow via the ASGI app (worker/network stubbed out)
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, worker
    from app.torbox_client import client as tb

    async def _noop_run():
        return

    async def _ok():
        return True

    monkeypatch.setattr(worker, "run", _noop_run)
    monkeypatch.setattr(worker, "housekeeping", _noop_run)
    monkeypatch.setattr(worker, "repair_paths", lambda: None)
    monkeypatch.setattr(worker, "resume_interrupted", lambda: None)
    monkeypatch.setattr(tb, "validate_key", _ok)
    with TestClient(main.app) as c:
        yield c


def test_unauthenticated_gets_403(client):
    assert client.get("/api/v2/torrents/info").status_code == 403


def test_login_then_authorized_then_logout(client):
    from app.config import settings
    bad = client.post("/api/v2/auth/login",
                      data={"username": "admin", "password": "nope"})
    assert bad.text == "Fails."

    ok = client.post("/api/v2/auth/login",
                     data={"username": settings.qbit_user, "password": settings.qbit_pass})
    assert ok.text == "Ok."
    assert "SID" in ok.cookies

    info = client.get("/api/v2/torrents/info")
    assert info.status_code == 200
    assert isinstance(info.json(), list)

    client.post("/api/v2/auth/logout")
    assert client.get("/api/v2/torrents/info").status_code == 403


# --------------------------------------------------------------------------- #
# local deletion follows the same layout the worker wrote
# --------------------------------------------------------------------------- #
def _seed(rel: str) -> str:
    """Create a file under the real (temp) download dir and return its path."""
    from app import worker
    dest = worker._safe_dest("radarr", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(b"x")
    return dest


def test_delete_local_removes_the_folder_made_for_loose_files():
    from app import worker
    from app.store import Torrent

    t = Torrent(hash="a" * 40, name="Del.Loose.S01E01", category="radarr",
                files=[{"id": 1, "name": "a.mkv"}, {"id": 2, "name": "b.nfo"}])
    a = _seed("Del.Loose.S01E01/a.mkv")
    _seed("Del.Loose.S01E01/b.nfo")
    folder = os.path.dirname(a)

    qbit_api._delete_local(t)
    assert not os.path.exists(folder)


def test_delete_local_removes_a_torrents_own_folder():
    from app.store import Torrent

    t = Torrent(hash="b" * 40, name="X", category="radarr",
                files=[{"id": 1, "name": "DelPack/a.mkv"}, {"id": 2, "name": "DelPack/b.mkv"}])
    a = _seed("DelPack/a.mkv")
    _seed("DelPack/b.mkv")
    folder = os.path.dirname(a)

    qbit_api._delete_local(t)
    assert not os.path.exists(folder)


# --------------------------------------------------------------------------- #
# what Sonarr/Radarr need before they will remove an imported download
# --------------------------------------------------------------------------- #
def test_completed_torrent_reads_as_done_seeding():
    """HasReachedSeedLimit() only consults a limit when it is >= 0; -1 means
    unlimited and is skipped entirely, so the item is never removable and
    CanMoveFiles stays false (turning every import into a copy)."""
    from app.store import STATE_COMPLETED, Torrent

    q = qbit_api._to_qbit(Torrent(hash="a" * 40, name="x", category="sonarr",
                                  size=100, state=STATE_COMPLETED,
                                  completion_on=1, local_progress=1.0))
    assert q["ratio_limit"] == 0 and q["ratio"] == 0.0        # limit - ratio <= 0.001
    assert q["seeding_time_limit"] == 0 and q["seeding_time"] == 0  # seeding_time >= limit
    assert q["progress"] == 1.0
    # Unlimited, so a global inactive-seeding limit can't make this removable
    # for the wrong reason.
    assert q["inactive_seeding_time_limit"] == -1
    assert q["last_activity"] > 0


def test_completed_state_is_pausedup_for_version_compatibility():
    """stoppedUP is only understood from Sonarr v4.0.5.1710 / Radarr v5.5.3.8819.
    Older builds treat an unknown state as still Downloading and never import at
    all, so this must stay pausedUP."""
    from app.store import STATE_COMPLETED, Torrent

    assert qbit_api._qbit_state(Torrent(hash="a" * 40, name="x",
                                        state=STATE_COMPLETED)) == "pausedUP"


def test_incomplete_torrent_is_not_reported_as_finished():
    """The seed limits say "no seeding required"; they must not make an
    unfinished download look importable."""
    from app.store import STATE_DOWNLOADING, Torrent

    q = qbit_api._to_qbit(Torrent(hash="a" * 40, name="x", category="sonarr",
                                  size=100, state=STATE_DOWNLOADING,
                                  local_progress=0.5))
    assert q["progress"] < 1.0
    assert q["state"] == "downloading"
    assert q["amount_left"] > 0
