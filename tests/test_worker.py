import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import runtime, worker
from app.store import (
    STATE_CLOUD,
    STATE_COMPLETED,
    STATE_DOWNLOADING,
    STATE_ERROR,
    STATE_QUEUED,
    Torrent,
)


# --------------------------------------------------------------------------- #
# path safety
# --------------------------------------------------------------------------- #
def test_safe_dest_allows_normal_paths():
    dest = worker._safe_dest("radarr", "Movie (2026)/movie.mkv")
    root = os.path.realpath(worker.settings.download_dir)
    assert dest.startswith(root + os.sep)


@pytest.mark.parametrize("category,rel", [
    ("radarr", "../../etc/passwd"),
    ("radarr", "/etc/passwd"),
    ("../../etc", "x"),
    ("radarr", "sub/../../../../tmp/evil"),
])
def test_safe_dest_rejects_escapes(category, rel):
    with pytest.raises(ValueError):
        worker._safe_dest(category, rel)


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_content_path_single_root_folder():
    t = Torrent(hash="a" * 40, name="X", category="radarr")
    files = [{"name": "Movie (2026)/a.mkv"}, {"name": "Movie (2026)/b.nfo"}]
    cp = worker._content_path(t, files)
    assert cp.endswith(os.path.join("radarr", "Movie (2026)"))


def test_content_path_multiple_roots_falls_back_to_base():
    t = Torrent(hash="a" * 40, name="X", category="radarr")
    files = [{"name": "a.mkv"}, {"name": "b.mkv"}]
    assert worker._content_path(t, files) == worker._save_path("radarr")


def test_map_files_normalises_backslashes_and_size():
    entry = {"files": [{"id": 1, "name": "d\\sub\\f.mkv", "size": "10"}]}
    out = worker._map_files(entry)
    assert out == [{"id": 1, "name": "d/sub/f.mkv", "size": 10}]


def test_apply_cloud_update_flags_failure_state():
    t = Torrent(hash="a" * 40, name="x")
    worker._apply_cloud_update(t, {"id": 1, "download_state": "cberror"})
    from app.store import STATE_ERROR
    assert t.state == STATE_ERROR


# --------------------------------------------------------------------------- #
# rate limiter
# --------------------------------------------------------------------------- #
async def test_rate_limiter_enforces_aggregate_rate():
    rl = worker._RateLimiter(10 * (1 << 20))  # 10 MiB/s
    start = time.monotonic()
    for _ in range(30):  # 30 MiB
        await rl.throttle(1 << 20)
    elapsed = time.monotonic() - start
    assert 2.5 < elapsed < 4.5  # ~3s


async def test_rate_limiter_unlimited_is_instant():
    rl = worker._RateLimiter(0)
    start = time.monotonic()
    for _ in range(1000):
        await rl.throttle(1 << 20)
    assert time.monotonic() - start < 0.5


async def test_rate_limiter_follows_runtime_setting(monkeypatch):
    rl = worker._RateLimiter()  # no fixed rate -> reads the runtime setting
    monkeypatch.setitem(runtime._values, "max_download_speed", 0)
    start = time.monotonic()
    for _ in range(100):
        await rl.throttle(1 << 20)
    assert time.monotonic() - start < 0.5

    monkeypatch.setitem(runtime._values, "max_download_speed", 10)  # MiB/s
    start = time.monotonic()
    for _ in range(20):  # 20 MiB at 10 MiB/s ~ 2s
        await rl.throttle(1 << 20)
    assert 1.5 < time.monotonic() - start < 3.5


# --------------------------------------------------------------------------- #
# restart recovery + retry state
# --------------------------------------------------------------------------- #
def test_resume_interrupted_requeues_downloading(worker_env):
    worker_env.upsert(Torrent(hash="a" * 40, name="x", state=STATE_DOWNLOADING, local_progress=0.9))
    worker.resume_interrupted()
    assert worker_env.get("a" * 40).state == STATE_CLOUD


def test_forget_clears_state(worker_env):
    h = "a" * 40
    worker._downloading.add(h)
    worker._attempts[h] = 2
    worker.forget(h)
    assert h not in worker._downloading and h not in worker._attempts


# --------------------------------------------------------------------------- #
# cloud cleanup + parallel gate (sync_once)
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, entries=None):
        self.entries = entries or []
        self.deleted = []

    async def my_list(self):
        return self.entries

    async def control(self, tid, op):
        self.deleted.append((tid, op))


async def test_cleanup_deletes_old_cloud_copy_keeps_recent(worker_env, monkeypatch):
    # torbox_cleanup_hours defaults to 24 (Settings is frozen, so rely on default).
    fake = _FakeClient()
    monkeypatch.setattr(worker, "client", fake)
    now = int(time.time())
    worker_env.upsert(Torrent(hash="a" * 40, name="old", category="radarr", torbox_id=42,
                              state=STATE_COMPLETED, completion_on=now - 25 * 3600))
    worker_env.upsert(Torrent(hash="b" * 40, name="new", category="radarr", torbox_id=43,
                              state=STATE_COMPLETED, completion_on=now - 3600))
    await worker.sync_once()
    assert fake.deleted == [(42, "delete")]
    assert worker_env.get("a" * 40).torbox_id is None
    assert worker_env.get("b" * 40).torbox_id == 43
    assert any(e["event"] == "cloud_removed" for e in worker_env.history())


async def test_parallel_torrent_gate(worker_env, monkeypatch):
    entry = {"id": 1, "hash": "a" * 40, "name": "x", "size": 100, "progress": 1.0,
             "download_finished": True, "download_present": True,
             "files": [{"id": 1, "name": "f.mkv", "size": 100}]}
    monkeypatch.setattr(worker, "client", _FakeClient([entry]))
    # max_parallel_torrents defaults to 2; fill both slots.
    worker._downloading.update({"x1", "x2"})  # both slots busy
    worker_env.upsert(Torrent(hash="a" * 40, name="x", category="radarr",
                              torbox_id=1, state=STATE_CLOUD))
    await worker.sync_once()
    # Slots full -> not started, stays out of the downloading set.
    assert worker_env.get("a" * 40).state == STATE_CLOUD
    assert "a" * 40 not in worker._downloading


# --------------------------------------------------------------------------- #
# the headline fix: TaskGroup cancels siblings on failure
# --------------------------------------------------------------------------- #
async def test_download_failure_cancels_sibling_and_requeues(worker_env, monkeypatch):
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING,
                              files=[{"id": 1, "name": "good", "size": 10},
                                     {"id": 2, "name": "bad", "size": 10}]))
    sibling_cancelled = {"v": False}

    async def fake_dl(t, f, progress):
        if f["name"] == "bad":
            await asyncio.sleep(0.02)
            raise IOError("boom")
        try:
            await asyncio.sleep(10)  # would hang forever if not cancelled
        except asyncio.CancelledError:
            sibling_cancelled["v"] = True
            raise

    monkeypatch.setattr(worker, "_download_file", fake_dl)
    worker._downloading.add(h)
    await worker._download_torrent(h)

    assert sibling_cancelled["v"] is True          # no orphaned writer left running
    assert worker_env.get(h).state == STATE_CLOUD  # first failure -> retry, not error
    assert h not in worker._downloading


class _FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        pass

    async def aiter_bytes(self, n):
        yield self._data


class _FakeStreamCM:
    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return _FakeResp(self._data)

    async def __aexit__(self, *a):
        return False


async def test_download_file_real_path_writes_and_counts(worker_env, monkeypatch):
    # Exercises the real _download_file body — semaphore, open/write, progress —
    # which the higher-level tests stub out. Guards the lazy-semaphore wiring.
    data = b"x" * 4096

    async def fake_request_dl(tid, fid):
        return "http://fake/f"

    monkeypatch.setattr(worker.client, "request_dl", fake_request_dl)
    monkeypatch.setattr(worker.client, "stream", lambda url, headers=None: _FakeStreamCM(data))
    t = Torrent(hash="a" * 40, name="x", category="radarr", torbox_id=1,
                files=[{"id": 1, "name": "unit_dl.bin", "size": len(data)}])
    progress = {"done": 0}
    await worker._download_file(t, t.files[0], progress)
    dest = worker._safe_dest("radarr", "unit_dl.bin")
    with open(dest, "rb") as fh:
        assert fh.read() == data
    assert progress["done"] == len(data)


# --------------------------------------------------------------------------- #
# housekeeping: timestamp parsing, age cleanup, subscription
# --------------------------------------------------------------------------- #
def test_parse_time():
    assert worker._parse_time("1970-01-01T00:00:10+00:00") == 10
    assert worker._parse_time("1970-01-01T00:00:10Z") == 10
    assert worker._parse_time("1970-01-01T00:00:10") == 10  # naive -> UTC
    assert worker._parse_time(5) == 5.0
    assert worker._parse_time("garbage") is None
    assert worker._parse_time("") is None
    assert worker._parse_time(None) is None


def _iso_days_ago(days: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()


async def test_age_cleanup_removes_old_spares_recent(worker_env, monkeypatch):
    fake = _FakeClient([
        {"id": 1, "hash": "c" * 40, "name": "ancient", "created_at": _iso_days_ago(31), "size": 5},
        {"id": 2, "hash": "d" * 40, "name": "fresh", "created_at": _iso_days_ago(2)},
        {"id": 3, "hash": "e" * 40, "name": "undated"},  # unparsable age -> spared
    ])
    monkeypatch.setattr(worker, "client", fake)
    monkeypatch.setitem(runtime._values, "cloud_max_age_days", 30)
    await worker.cleanup_aged_cloud()
    assert fake.deleted == [(1, "delete")]
    events = worker_env.history()
    assert any(e["event"] == "cloud_removed" and e["name"] == "ancient" for e in events)


async def test_age_cleanup_disabled_by_default(worker_env, monkeypatch):
    fake = _FakeClient([{"id": 1, "hash": "c" * 40, "name": "old", "created_at": _iso_days_ago(400)}])
    monkeypatch.setattr(worker, "client", fake)
    await worker.cleanup_aged_cloud()  # cloud_max_age_days defaults to 0 = off
    assert fake.deleted == []


async def test_age_cleanup_spares_active_local_pull(worker_env, monkeypatch):
    h = "a" * 40
    fake = _FakeClient([{"id": 7, "hash": h, "name": "busy", "created_at": _iso_days_ago(60)}])
    monkeypatch.setattr(worker, "client", fake)
    monkeypatch.setitem(runtime._values, "cloud_max_age_days", 30)
    worker_env.upsert(Torrent(hash=h, name="busy", torbox_id=7, state=STATE_DOWNLOADING))
    worker._downloading.add(h)
    await worker.cleanup_aged_cloud()
    assert fake.deleted == []


async def test_age_cleanup_marks_tracked_incomplete_as_error(worker_env, monkeypatch):
    h = "a" * 40
    fake = _FakeClient([{"id": 7, "hash": h, "name": "stuck", "created_at": _iso_days_ago(60)}])
    monkeypatch.setattr(worker, "client", fake)
    monkeypatch.setitem(runtime._values, "cloud_max_age_days", 30)
    worker_env.upsert(Torrent(hash=h, name="stuck", torbox_id=7, state=STATE_CLOUD))
    await worker.cleanup_aged_cloud()
    assert fake.deleted == [(7, "delete")]
    t = worker_env.get(h)
    assert t.state == STATE_ERROR
    assert t.torbox_id is None


async def test_age_cleanup_keeps_tracked_completed_entry(worker_env, monkeypatch):
    h = "a" * 40
    fake = _FakeClient([{"id": 7, "hash": h, "name": "done", "created_at": _iso_days_ago(60)}])
    monkeypatch.setattr(worker, "client", fake)
    monkeypatch.setitem(runtime._values, "cloud_max_age_days", 30)
    worker_env.upsert(Torrent(hash=h, name="done", torbox_id=7, state=STATE_COMPLETED))
    await worker.cleanup_aged_cloud()
    t = worker_env.get(h)
    assert t.state == STATE_COMPLETED  # local files / import tracking untouched
    assert t.torbox_id is None


async def test_refresh_subscription_populates_status(worker_env, monkeypatch):
    expires = datetime.now(tz=timezone.utc) + timedelta(days=5)

    async def fake_me():
        return {"plan": 2, "premium_expires_at": expires.isoformat()}

    monkeypatch.setattr(worker.client, "user_me", fake_me)
    notified = []

    async def fake_notify(days_left, expires_at):
        notified.append(days_left)

    monkeypatch.setattr(worker.notify, "maybe_notify_subscription", fake_notify)
    await worker.refresh_subscription()
    sub = worker.subscription_status()
    assert sub["plan"] == 2
    assert 4.5 <= sub["days_left"] <= 5.0
    assert notified and 4.9 < notified[0] <= 5.0


async def test_refresh_subscription_survives_api_failure(worker_env, monkeypatch):
    async def boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(worker.client, "user_me", boom)
    await worker.refresh_subscription()
    assert worker.subscription_status() is None


async def test_download_success_completes(worker_env, monkeypatch):
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING,
                              files=[{"id": 1, "name": "f.mkv", "size": 10}]))

    async def fake_dl(t, f, progress):
        progress["done"] += f["size"]

    monkeypatch.setattr(worker, "_download_file", fake_dl)
    worker._downloading.add(h)
    await worker._download_torrent(h)

    done = worker_env.get(h)
    assert done.state == STATE_COMPLETED
    assert done.local_progress == 1.0
    assert done.completion_on > 0
    assert any(e["event"] == "downloaded" for e in worker_env.history())
