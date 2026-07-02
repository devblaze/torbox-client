import asyncio
import os
import time

import pytest

from app import worker
from app.store import (
    STATE_CLOUD,
    STATE_COMPLETED,
    STATE_DOWNLOADING,
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
