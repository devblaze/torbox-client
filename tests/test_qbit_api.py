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
