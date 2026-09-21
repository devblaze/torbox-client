import asyncio
import dataclasses
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


def test_content_path_single_file_keeps_torbox_layout():
    t = Torrent(hash="a" * 40, name="X", category="radarr")
    files = [{"name": "movie.mkv"}]
    assert worker._content_path(t, files) == os.path.join(worker._save_path("radarr"), "movie.mkv")
    assert worker._root_folder(t.name, files) == ""


def test_content_path_multiple_roots_gets_its_own_folder():
    """Sonarr refuses a completed torrent whose content_path is the client's
    base download dir, so loose files need a folder made for them."""
    t = Torrent(hash="a" * 40, name="Show.S01E01", category="radarr")
    files = [{"name": "a.mkv"}, {"name": "b.nfo"}]
    base = worker._save_path("radarr")
    assert worker._content_path(t, files) == os.path.join(base, "Show.S01E01")
    assert worker._content_path(t, files) != base
    assert worker._root_folder(t.name, files) == "Show.S01E01"


def test_content_path_without_files_is_still_below_base():
    t = Torrent(hash="a" * 40, name="Nothing Yet", category="radarr")
    base = worker._save_path("radarr")
    assert worker._content_path(t, []) == os.path.join(base, "Nothing Yet")


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "a/b", "..", ".", "   ", "", "C:\\evil\\x", "  ..  ",
])
def test_as_segment_stays_one_harmless_segment(name):
    """A torrent name comes from TorBox, so it has to be safe to use as a
    directory name — one segment, never empty, never a traversal."""
    seg = worker._as_segment(name)
    assert seg and "/" not in seg and "\\" not in seg
    assert seg not in (".", "..")
    # And it survives the path guard rather than blowing up a download.
    worker._safe_dest("radarr", os.path.join(seg, "f.mkv"))


def test_as_segment_keeps_ordinary_names_intact():
    assert worker._as_segment("Show.S01E01.1080p-GRP") == "Show.S01E01.1080p-GRP"


def test_file_dest_puts_loose_files_under_the_made_folder():
    t = Torrent(hash="a" * 40, name="Show.S01E01", category="radarr",
                files=[{"id": 1, "name": "a.mkv"}, {"id": 2, "name": "b.nfo"}])
    dest = worker._file_dest(t, t.files[0])
    assert dest == worker._safe_dest("radarr", "Show.S01E01/a.mkv")


def test_file_dest_leaves_a_torrents_own_folder_alone():
    t = Torrent(hash="a" * 40, name="X", category="radarr",
                files=[{"id": 1, "name": "Pack/a.mkv"}, {"id": 2, "name": "Pack/b.mkv"}])
    assert worker._file_dest(t, t.files[0]) == worker._safe_dest("radarr", "Pack/a.mkv")


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
        self.list_calls = 0

    async def my_list(self):
        self.list_calls += 1
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
    # Cleanup deletes by torbox_id, so it never needed the listing.
    assert fake.list_calls == 0


async def test_sync_once_skips_poll_when_nothing_actionable(worker_env, monkeypatch):
    """Completed/errored rows are kept so the *arr apps can still import them,
    but they are never matched against a mylist entry — polling for those alone
    is a full uncached listing of the account with nothing to do."""
    fake = _FakeClient()
    monkeypatch.setattr(worker, "client", fake)
    worker_env.upsert(Torrent(hash="a" * 40, name="done", state=STATE_COMPLETED,
                              torbox_id=None, completion_on=int(time.time())))
    worker_env.upsert(Torrent(hash="b" * 40, name="bad", state=STATE_ERROR, torbox_id=None))
    await worker.sync_once()
    assert fake.list_calls == 0


async def test_sync_once_polls_when_something_is_actionable(worker_env, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(worker, "client", fake)
    worker_env.upsert(Torrent(hash="a" * 40, name="done", state=STATE_COMPLETED, torbox_id=None))
    worker_env.upsert(Torrent(hash="b" * 40, name="busy", state=STATE_CLOUD, torbox_id=7))
    await worker.sync_once()
    assert fake.list_calls == 1


async def test_sync_once_empty_store_skips_poll(worker_env, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(worker, "client", fake)
    await worker.sync_once()
    assert fake.list_calls == 0


class _SlowClient(_FakeClient):
    """A client whose my_list() takes long enough for a download task to finish."""

    def __init__(self, entries=None, on_list=None):
        super().__init__(entries)
        self._on_list = on_list

    async def my_list(self):
        self.list_calls += 1
        if self._on_list:
            self._on_list()          # the pull finishes mid-flight
        return self.entries


def _ready_entry(h):
    return {"id": 1, "hash": h, "name": "x", "size": 100, "progress": 1.0,
            "download_finished": True, "download_present": True,
            "files": [{"id": 1, "name": "f.mkv", "size": 100}]}


async def test_completion_during_mylist_is_not_reverted(worker_env, monkeypatch):
    """sync_once snapshots the store, then awaits mylist. A download task that
    finishes inside that window had its COMPLETED row overwritten by the stale
    snapshot, leaving the torrent stuck in 'downloading' with nothing behind
    it — and the start gate then refuses to restart it, so it never recovers."""
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING, local_progress=0.98,
                              files=[{"id": 1, "name": "f.mkv", "size": 100}]))

    def finish_the_pull():
        done = worker_env.get(h)
        done.state = STATE_COMPLETED
        done.local_progress = 1.0
        done.completion_on = int(time.time())
        worker_env.upsert(done)

    monkeypatch.setattr(worker, "client",
                        _SlowClient([_ready_entry(h)], on_list=finish_the_pull))
    await worker.sync_once()

    t = worker_env.get(h)
    assert t.state == STATE_COMPLETED
    assert t.local_progress == 1.0


async def test_requeue_during_mylist_is_not_reverted(worker_env, monkeypatch):
    """Same race from the other side: the stall watchdog puts a torrent back to
    the cloud state mid-listing, and the stale snapshot wrote 'downloading'
    over it — wedging the very torrent the watchdog just rescued."""
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING, local_progress=0.98,
                              files=[{"id": 1, "name": "f.mkv", "size": 100}]))

    def watchdog_requeues():
        cur = worker_env.get(h)
        cur.state = STATE_CLOUD
        cur.dlspeed = 0
        worker_env.upsert(cur)

    monkeypatch.setattr(worker, "_download_file", lambda *a, **k: None)
    monkeypatch.setattr(worker, "client",
                        _SlowClient([_ready_entry(h)], on_list=watchdog_requeues))
    await worker.sync_once()

    # Picked up for another attempt rather than written back to 'downloading'
    # with no task behind it.
    assert h in worker._downloading
    await worker.shutdown()


async def test_orphaned_downloading_row_is_requeued(worker_env, monkeypatch):
    """Recovery for rows already wedged by an older build: 'downloading' with no
    entry in _downloading means no task exists. resume_interrupted() only runs
    at startup, so without this they stay stuck until the container restarts."""
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING, local_progress=0.98,
                              files=[{"id": 1, "name": "f.mkv", "size": 100}]))
    monkeypatch.setattr(worker, "_download_file", lambda *a, **k: None)
    monkeypatch.setattr(worker, "client", _FakeClient([_ready_entry(h)]))
    assert h not in worker._downloading

    await worker.sync_once()
    assert h in worker._downloading
    await worker.shutdown()


async def test_live_pull_is_left_alone(worker_env, monkeypatch):
    """The recovery must not disturb a torrent that really is being pulled."""
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING, local_progress=0.5,
                              files=[{"id": 1, "name": "f.mkv", "size": 100}]))
    worker._downloading.add(h)  # a task is running
    monkeypatch.setattr(worker, "client", _FakeClient([_ready_entry(h)]))

    await worker.sync_once()
    t = worker_env.get(h)
    assert t.state == STATE_DOWNLOADING
    assert t.local_progress == 0.5


# --------------------------------------------------------------------------- #
# retry backoff before a failure is reported to the *arr app
# --------------------------------------------------------------------------- #
def test_retry_delay_is_immediate_then_doubles():
    """The first rounds clear a momentary blip; after that, real time has to
    pass, because the outages that need retrying take minutes."""
    base = worker.settings.torrent_retry_backoff
    cap = worker.settings.torrent_retry_backoff_max
    assert [worker._retry_delay(n) for n in (1, 2, 3)] == [0.0, 0.0, 0.0]
    assert worker._retry_delay(4) == base
    assert worker._retry_delay(5) == base * 2
    assert worker._retry_delay(6) == base * 4
    # ...and never longer than the cap.
    assert worker._retry_delay(99) == cap


def _with_settings(monkeypatch, **overrides):
    """Swap in a settings copy — Settings is frozen, so it can't be patched."""
    monkeypatch.setattr(worker, "settings",
                        dataclasses.replace(worker.settings, **overrides))


def test_retry_delay_respects_a_lowered_cap(monkeypatch):
    _with_settings(monkeypatch, torrent_retry_backoff_max=60)
    assert worker._retry_delay(99) <= 60


async def test_failed_round_holds_off_the_next_attempt(worker_env, monkeypatch):
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING,
                              files=[{"id": 1, "name": "f.mkv", "size": 10}]))

    async def boom(t, f, progress):
        raise IOError("nope")

    monkeypatch.setattr(worker, "_download_file", boom)
    monkeypatch.setattr(worker, "_retry_delay", lambda n: 600.0)
    worker._downloading.add(h)
    await worker._download_torrent(h)

    assert worker_env.get(h).state == STATE_CLOUD
    assert worker._retry_after[h] > time.time() + 500

    # The sync loop must leave it alone until the deadline passes.
    monkeypatch.setattr(worker, "client", _FakeClient([_ready_entry(h)]))
    await worker.sync_once()
    assert h not in worker._downloading

    worker._retry_after[h] = time.time() - 1  # deadline reached
    monkeypatch.setattr(worker, "_download_file", lambda *a, **k: None)
    await worker.sync_once()
    assert h in worker._downloading
    await worker.shutdown()


async def test_error_is_reported_only_after_the_configured_rounds(worker_env, monkeypatch):
    """Sonarr must not be told it failed until we have genuinely given up."""
    h = "a" * 40
    monkeypatch.setattr(worker, "_TORRENT_RETRY_LIMIT", 3)
    monkeypatch.setattr(worker, "_retry_delay", lambda n: 0.0)

    async def boom(t, f, progress):
        raise IOError("nope")

    monkeypatch.setattr(worker, "_download_file", boom)
    for round_ in (1, 2):
        worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                                  state=STATE_DOWNLOADING,
                                  files=[{"id": 1, "name": "f.mkv", "size": 10}]))
        worker._downloading.add(h)
        await worker._download_torrent(h)
        assert worker_env.get(h).state == STATE_CLOUD, f"round {round_} gave up early"

    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING,
                              files=[{"id": 1, "name": "f.mkv", "size": 10}]))
    worker._downloading.add(h)
    await worker._download_torrent(h)
    assert worker_env.get(h).state == STATE_ERROR


# --------------------------------------------------------------------------- #
# a finished cloud copy with no file list must not spin forever
# --------------------------------------------------------------------------- #
def _fileless_entry(h):
    e = _ready_entry(h)
    e["files"] = []
    return e


async def test_fileless_torrent_waits_then_fails(worker_env, monkeypatch):
    """_download_torrent bails before it logs when there are no files, so this
    used to re-spawn silently on every poll — one torrent did for 45 days."""
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_CLOUD, files=[]))
    monkeypatch.setattr(worker, "client", _FakeClient([_fileless_entry(h)]))

    await worker.sync_once()
    t = worker_env.get(h)
    assert t.state == STATE_CLOUD          # still inside the grace period
    assert h not in worker._downloading    # and not spinning up a doomed task

    # Pretend the grace period has elapsed.
    worker._fileless_since[h] = time.time() - worker.settings.fileless_timeout - 1
    await worker.sync_once()
    t = worker_env.get(h)
    assert t.state == STATE_ERROR
    assert "no file list" in t.error
    assert any(e["event"] == "error" for e in worker_env.history())


async def test_fileless_timeout_can_be_disabled(worker_env, monkeypatch):
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_CLOUD, files=[]))
    monkeypatch.setattr(worker, "client", _FakeClient([_fileless_entry(h)]))
    _with_settings(monkeypatch, fileless_timeout=0)
    worker._fileless_since[h] = time.time() - 99999
    await worker.sync_once()
    assert worker_env.get(h).state == STATE_CLOUD


async def test_files_arriving_late_clears_the_fileless_clock(worker_env, monkeypatch):
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_CLOUD, files=[]))
    monkeypatch.setattr(worker, "client", _FakeClient([_fileless_entry(h)]))
    await worker.sync_once()
    assert h in worker._fileless_since

    monkeypatch.setattr(worker, "client", _FakeClient([_ready_entry(h)]))
    monkeypatch.setattr(worker, "_download_file", lambda *a, **k: None)
    await worker.sync_once()
    assert h not in worker._fileless_since
    assert worker_env.get(h).state == STATE_DOWNLOADING
    await worker.shutdown()


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


# --------------------------------------------------------------------------- #
# stall watchdog: a pull that stops getting anywhere must not hold a slot
# --------------------------------------------------------------------------- #
def test_stall_floor_is_flat_when_uncapped(monkeypatch):
    monkeypatch.setitem(runtime._values, "max_download_speed", 0)
    assert worker._stall_floor(90) == worker._STALL_FLOOR_BYTES


def test_stall_floor_scales_down_to_a_configured_cap(monkeypatch):
    monkeypatch.setitem(runtime._values, "max_download_speed", 0.01)  # ~10 KiB/s
    # A quarter of what the cap allows in the window, so the limiter itself
    # can never be mistaken for a stall.
    assert worker._stall_floor(90) == int(0.01 * (1 << 20) * 90 / 4)
    assert worker._stall_floor(90) < worker._STALL_FLOOR_BYTES


def test_stall_floor_never_reaches_zero(monkeypatch):
    monkeypatch.setitem(runtime._values, "max_download_speed", 0.000001)
    assert worker._stall_floor(1) >= 1


def _fake_clock(monkeypatch):
    """A monotonic clock the test advances by hand.

    Time only moves when a download coroutine moves it, so the watchdog can
    never fire merely because a loaded CI runner starved the event loop — if
    the downloader doesn't run, no time passes.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(worker, "_now", lambda: clock["t"])
    monkeypatch.setattr(worker, "_PROGRESS_TICK", 0.001)
    monkeypatch.setattr(worker, "_stall_window", lambda: 1.0)
    return clock


def _one_file_torrent(worker_env, h, size=1 << 30):
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING,
                              files=[{"id": 1, "name": "f.mkv", "size": size}]))


async def test_stalled_pull_is_cancelled_and_requeued(worker_env, monkeypatch):
    """The headline fix: a stream that opens, delivers almost nothing and never
    errors used to hold its MAX_PARALLEL_TORRENTS slot forever. httpx's read
    timeout can't see it, because bytes technically still arrive."""
    h = "a" * 40
    _one_file_torrent(worker_env, h)
    clock = _fake_clock(monkeypatch)
    cancelled = {"v": False}

    async def trickle(t, f, progress):
        progress["done"] += 16  # a token few bytes, then nothing ever again
        try:
            while True:
                await asyncio.sleep(0.001)
                clock["t"] += 0.5  # time passes; no bytes do
        except asyncio.CancelledError:
            cancelled["v"] = True
            raise

    monkeypatch.setattr(worker, "_download_file", trickle)
    worker._downloading.add(h)
    await worker._download_torrent(h)

    assert cancelled["v"] is True                   # the stream was torn down
    assert worker_env.get(h).state == STATE_CLOUD   # requeued for another round
    assert h not in worker._downloading             # and the slot is free again
    assert worker._attempts[h] == 1


async def test_slow_but_progressing_pull_is_left_alone(worker_env, monkeypatch):
    """Bytes arriving must push the deadline out, however long the pull runs.

    The clock advances half a window per step across twenty steps — ten windows
    of simulated time — so this genuinely exercises the reset rather than just
    finishing before the first window expires.
    """
    h = "a" * 40
    steps, per_step = 20, 2 << 20
    _one_file_torrent(worker_env, h, size=steps * per_step)
    clock = _fake_clock(monkeypatch)

    async def slow(t, f, progress):
        for _ in range(steps):
            await asyncio.sleep(0.001)
            clock["t"] += 0.5          # half a window
            progress["done"] += per_step  # comfortably over the 1 MiB floor

    monkeypatch.setattr(worker, "_download_file", slow)
    worker._downloading.add(h)
    await worker._download_torrent(h)

    assert clock["t"] >= 10.0  # ten windows elapsed without a false positive
    assert worker_env.get(h).state == STATE_COMPLETED


async def test_watchdog_disabled_by_zero_window(worker_env, monkeypatch):
    h = "a" * 40
    _one_file_torrent(worker_env, h, size=10)
    clock = _fake_clock(monkeypatch)
    monkeypatch.setattr(worker, "_stall_window", lambda: 0)

    async def silent(t, f, progress):
        for _ in range(10):
            await asyncio.sleep(0.001)
            clock["t"] += 100.0  # far past any window, and not a byte delivered
        progress["done"] += 10

    monkeypatch.setattr(worker, "_download_file", silent)
    worker._downloading.add(h)
    await worker._download_torrent(h)
    assert worker_env.get(h).state == STATE_COMPLETED


async def test_pull_is_cancelled_when_the_torrent_is_removed(worker_env, monkeypatch):
    """Deleting from Sonarr used to leave the file streams writing to disk —
    re-creating the very files torrents/delete had just removed."""
    h = "a" * 40
    worker_env.upsert(Torrent(hash=h, name="x", category="radarr", torbox_id=1,
                              state=STATE_DOWNLOADING,
                              files=[{"id": 1, "name": "f.mkv", "size": 1 << 30}]))
    cancelled = {"v": False}

    async def long_pull(t, f, progress):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["v"] = True
            raise

    async def remove_soon():
        await asyncio.sleep(0.02)
        worker_env.delete(h)

    monkeypatch.setattr(worker, "_download_file", long_pull)
    monkeypatch.setattr(worker, "_PROGRESS_TICK", 0.01)
    worker._downloading.add(h)
    async with asyncio.TaskGroup() as tg:
        tg.create_task(remove_soon())
        tg.create_task(worker._download_torrent(h))

    assert cancelled["v"] is True
    assert worker_env.get(h) is None
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
