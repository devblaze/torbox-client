"""Background sync between TorBox and the local download folder.

Loop:
  1. Poll TorBox ``mylist`` for every tracked torrent.
  2. Update cloud progress / size / file list in the store.
  3. When a torrent has finished in the cloud, pull each file to
     ``{download_dir}/{category}/{file_path}`` and, once every file is fully
     local, mark it completed so Sonarr/Radarr can import it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from . import notify, runtime
from .config import settings
from .store import (
    STATE_CLOUD,
    STATE_COMPLETED,
    STATE_DOWNLOADING,
    STATE_ERROR,
    STATE_QUEUED,
    Torrent,
    store,
)
from .torbox_client import client

log = logging.getLogger("worker")

# Torrents whose local download is currently running, so we never start twice.
_downloading: set[str] = set()
# Failed local-pull attempts per torrent; reset on success, capped below.
_attempts: dict[str, int] = {}
_TORRENT_RETRY_LIMIT = settings.torrent_retry_limit
# Rounds that retry at once before the backoff schedule starts; a momentary CDN
# blip clears inside these, anything longer needs real time to pass.
_FAST_RETRY_ROUNDS = 3
# hash -> epoch seconds before which the next round must not start.
_retry_after: dict[str, float] = {}
# hash -> when we first saw a finished cloud copy with an empty file list.
_fileless_since: dict[str, float] = {}

_FAILED_STATES = {"failed", "error", "cberror", "uploaderror"}

# Seconds between local-progress writes (and stall-watchdog checks).
_PROGRESS_TICK = 3
# Bytes a torrent's local pull must gain inside one watchdog window to count as
# alive. settings.stall_timeout is enforced by httpx as a *read* timeout, which
# only fires when a socket goes completely silent — a CDN stream that trickles a
# few KiB keeps it happy indefinitely while the torrent sits just short of done
# forever, holding a MAX_PARALLEL_TORRENTS slot nothing else can use. Measuring
# actual bytes catches the trickle as well as the silence.
_STALL_FLOOR_BYTES = 1 << 20  # 1 MiB


def _now() -> float:
    """Monotonic clock for the stall watchdog.

    Indirected so tests can drive the watchdog from a clock they control,
    instead of racing real sleeps against a real deadline on a loaded machine.
    """
    return time.monotonic()

# Caps concurrent file streams across all torrents. Created lazily on the running
# loop (asyncio primitives bind to the loop that first awaits them).
_file_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _semaphore() -> asyncio.Semaphore:
    global _file_semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _file_semaphore is None or _semaphore_loop is not loop:
        _file_semaphore = asyncio.Semaphore(settings.max_parallel_downloads)
        _semaphore_loop = loop
    return _file_semaphore


class _RateLimiter:
    """Leaky-bucket limiter shared by all file streams (aggregate cap).

    With no fixed rate it follows the ``max_download_speed`` runtime setting on
    every call, so a change saved in the web UI applies mid-download.
    """

    def __init__(self, rate_bytes_per_s: float | None = None):
        self.rate = rate_bytes_per_s
        self._next_free = 0.0
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def throttle(self, nbytes: int) -> None:
        rate = self.rate if self.rate is not None else runtime.get("max_download_speed") * (1 << 20)
        if rate <= 0:
            return
        async with self._get_lock():
            now = time.monotonic()
            self._next_free = max(self._next_free, now) + nbytes / rate
            wait = self._next_free - now
        if wait > 0:
            await asyncio.sleep(wait)


_rate = _RateLimiter()

# Strong references to detached tasks so the event loop can't GC them mid-flight
# (asyncio only holds a weak reference to the task returned by create_task).
_background_tasks: set[asyncio.Task] = set()

# Wall-clock of the last completed sync loop iteration; surfaced by /health so a
# wedged worker is observable. Seeded at import so startup isn't reported stale.
_last_tick: float = time.time()


def _spawn(coro) -> asyncio.Task:
    """create_task that keeps a strong reference until the task finishes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def last_tick() -> float:
    return _last_tick


def forget(hash_: str) -> None:
    """Drop per-torrent worker state when a torrent is removed."""
    _downloading.discard(hash_)
    _attempts.pop(hash_, None)
    _retry_after.pop(hash_, None)
    _fileless_since.pop(hash_, None)


def _retry_delay(round_: int) -> float:
    """Seconds to hold off before local-pull round ``round_ + 1``."""
    if round_ <= _FAST_RETRY_ROUNDS:
        return 0.0
    step = round_ - _FAST_RETRY_ROUNDS - 1
    return float(min(settings.torrent_retry_backoff * (2 ** step),
                     settings.torrent_retry_backoff_max))


async def shutdown() -> None:
    """Cancel in-flight download tasks and wait for them to unwind.

    Must run before the shared httpx client is closed, otherwise closing it
    tears down live streams mid-write and leaves truncated files on disk.
    """
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _category_dir(category: str) -> str:
    """Local dir this container writes into."""
    return os.path.join(settings.download_dir, category) if category else settings.download_dir


def _safe_dest(category: str, rel: str) -> str:
    """Resolve ``download_dir/category/rel``, refusing paths that escape it.

    ``category`` comes from Sonarr/Radarr and ``rel`` from the TorBox API; a
    ``..`` in either must never let us write or delete outside the downloads
    mount. ``realpath`` also collapses symlinks in the resolved target.
    """
    root = os.path.realpath(settings.download_dir)
    dest = os.path.realpath(os.path.join(root, category, rel))
    if dest != root and not dest.startswith(root + os.sep):
        raise ValueError(f"path escapes download dir: category={category!r} rel={rel!r}")
    return dest


def _save_path(category: str) -> str:
    """Path the *arr containers see (may differ from _category_dir via SAVE_PATH)."""
    return os.path.join(settings.save_path_base, category) if category else settings.save_path_base


def _top_segments(files: list[dict]) -> set[str]:
    """First path segment of every file — the torrent's root folder, if it has one."""
    return {n.replace("\\", "/").split("/", 1)[0]
            for n in (f.get("name", "") for f in files) if n}


def _as_segment(name: str) -> str:
    """A torrent name reduced to one safe path segment."""
    seg = name.replace("\\", "/").replace("/", "_").strip().strip(".")
    return seg or "torrent"


def _root_folder(name: str, files: list[dict]) -> str:
    """Folder inside the category dir to put this torrent's files in.

    Empty when the torrent's files already share one top-level segment — a
    single file, or a release that ships its own folder — because then that
    segment is the root and TorBox's layout is kept as-is.

    Otherwise we make a folder named after the torrent, the same way real
    qBittorrent does with ``create_subfolder_enabled``. Loose files written
    straight into the category dir have no root to point Sonarr/Radarr at, and
    they collide between torrents.
    """
    return "" if len(_top_segments(files)) == 1 else _as_segment(name)


def _content_path(t: Torrent, files: list[dict]) -> str:
    """Root file/folder path as the *arr apps should see it.

    Never the bare category dir: Sonarr/Radarr refuse to import a completed
    torrent whose content_path equals the download client's own base path
    ("Unable to Import. Path matches client base download directory").
    """
    base = _save_path(t.category)
    root = _root_folder(t.name, files)
    if root:
        return os.path.join(base, root)
    return os.path.join(base, next(iter(_top_segments(files))))


def _file_dest(t: Torrent, file: dict) -> str:
    """Local path a torrent's file is written to."""
    rel = os.path.join(_root_folder(t.name, t.files), file.get("name", ""))
    return _safe_dest(t.category, rel)


def _map_files(entry: dict) -> list[dict]:
    out = []
    for f in entry.get("files", []) or []:
        out.append({
            "id": f.get("id"),
            "name": (f.get("name") or f.get("short_name") or "").replace("\\", "/"),
            "size": int(f.get("size") or 0),
        })
    return out


def _apply_cloud_update(t: Torrent, entry: dict) -> None:
    t.torbox_id = entry.get("id", t.torbox_id)
    t.name = entry.get("name") or t.name
    t.size = int(entry.get("size") or t.size or 0)
    t.cloud_progress = float(entry.get("progress") or 0.0)
    t.dlspeed = int(entry.get("download_speed") or 0)
    files = _map_files(entry)
    if files:
        t.files = files
    dl_state = str(entry.get("download_state") or "").lower()
    if any(s in dl_state for s in _FAILED_STATES):
        t.state = STATE_ERROR
        t.error = entry.get("download_state") or "TorBox reported a failure"


async def _download_file(t: Torrent, file: dict, progress: dict) -> None:
    """Download a single TorBox file, resuming partials and retrying stalls.

    Each attempt gets a fresh CDN link (they expire after ~3h) and resumes
    from whatever is already on disk via a Range request. A stream that stops
    delivering bytes for STALL_TIMEOUT seconds raises and is retried instead
    of hanging the torrent forever.
    """
    file_id = file["id"]
    rel = file["name"]
    expected = int(file.get("size") or 0)
    dest = _file_dest(t, file)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

    existing = os.path.getsize(dest) if os.path.exists(dest) else 0
    if expected and existing > expected:  # corrupt/stale — start over
        os.remove(dest)
        existing = 0
    progress["done"] += existing  # bytes already on disk count toward progress
    if expected and existing == expected:
        return

    last_err: Exception | None = None
    for attempt in range(1, settings.download_retries + 1):
        try:
            async with _semaphore():
                url = await client.request_dl(t.torbox_id, file_id)
                offset = os.path.getsize(dest) if os.path.exists(dest) else 0
                headers = {"Range": f"bytes={offset}-"} if offset else None
                with open(dest, "ab" if offset else "wb") as fh:
                    async with client.stream(url, headers=headers) as resp:
                        if offset and resp.status_code == 200:
                            # Server ignored Range; rewrite from scratch.
                            fh.seek(0)
                            fh.truncate()
                            progress["done"] -= offset
                        resp.raise_for_status()
                        async for chunk in resp.aiter_bytes(1 << 20):  # 1 MiB
                            fh.write(chunk)
                            progress["done"] += len(chunk)
                            await _rate.throttle(len(chunk))
            got = os.path.getsize(dest) if os.path.exists(dest) else 0
            if expected and got < expected:
                raise IOError(f"short read: {got}/{expected}")
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < settings.download_retries:
                # repr() because e.g. httpx.ReadTimeout stringifies to ""
                log.warning("Attempt %d/%d for %s failed (%r) — resuming with a fresh link",
                            attempt, settings.download_retries, rel, exc)
                await asyncio.sleep(min(10 * attempt, 30))
    raise IOError(f"{rel} failed after {settings.download_retries} attempts: {last_err!r}")


def _stall_window() -> float:
    """Seconds of no real progress before a torrent's local pull is abandoned.

    Twice ``stall_timeout`` so that one full per-file retry cycle (the httpx
    read timeout plus its backoff) can play out and recover on its own before
    the torrent-level watchdog steps in. 0 disables the watchdog.
    """
    return max(settings.stall_timeout, 0) * 2


def _stall_floor(window: float) -> int:
    """Bytes that must arrive within ``window`` before we call a pull stalled.

    A configured speed cap can legitimately hold throughput under the flat
    floor, so never demand more than a quarter of what the cap allows.
    """
    floor = _STALL_FLOOR_BYTES
    cap = runtime.get("max_download_speed") * (1 << 20)  # bytes/s, 0 = unlimited
    if cap > 0:
        floor = min(floor, int(cap * window / 4))
    return max(floor, 1)


async def _download_torrent(hash_: str) -> None:
    t = store.get(hash_)
    if not t or not t.torbox_id or not t.files:
        # Revert the DB state the sync loop set before spawning us, otherwise the
        # torrent is stuck in 'downloading' with no task backing it.
        if t and t.state == STATE_DOWNLOADING:
            t.state = STATE_CLOUD
            store.upsert(t)
        _downloading.discard(hash_)
        return
    total = sum(int(f.get("size") or 0) for f in t.files) or t.size or 1
    progress = {"done": 0}
    started = time.time()
    log.info("Downloading %s (%d files, %.2f GiB)", t.name, len(t.files), total / (1 << 30))
    try:
        window = _stall_window()

        # A light periodic progress writer while files stream in, doubling as
        # the watchdog that ends a pull which has stopped getting anywhere.
        async def _report() -> None:
            last_done = progress["done"]
            mark_done = progress["done"]      # bytes at the start of this window
            mark_time = _now()
            while True:
                await asyncio.sleep(_PROGRESS_TICK)
                cur = store.get(hash_)
                if not cur:
                    return  # removed from under us; the caller cancels the pull
                cur.state = STATE_DOWNLOADING
                cur.local_progress = min(progress["done"] / total, 1.0)
                # Windowed speed: shows 0 during a stall instead of a decaying average.
                cur.dlspeed = max(int((progress["done"] - last_done) / _PROGRESS_TICK), 0)
                last_done = progress["done"]
                store.upsert(cur)

                if window <= 0:
                    continue
                gained = progress["done"] - mark_done
                if gained >= _stall_floor(window):
                    mark_done, mark_time = progress["done"], _now()
                elif _now() - mark_time >= window:
                    raise TimeoutError(
                        f"only {gained / (1 << 10):.0f} KiB in "
                        f"{int(_now() - mark_time)}s")

        async def _pull_files() -> None:
            # TaskGroup (unlike gather) cancels the still-running file downloads
            # as soon as one fails, so a failed torrent leaves no detached tasks
            # writing to disk when we retry it — that used to corrupt files.
            async with asyncio.TaskGroup() as tg:
                for f in t.files:
                    tg.create_task(_download_file(t, f, progress))

        files = asyncio.create_task(_pull_files())
        reporter = asyncio.create_task(_report())
        try:
            finished, _ = await asyncio.wait(
                {files, reporter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Whichever finished, the other one is done here: cancelling the
            # pull also stops every file stream writing to disk.
            files.cancel()
            reporter.cancel()
            await asyncio.gather(files, reporter, return_exceptions=True)
        if files in finished:
            files.result()     # the pull won the race: success, or its own failure
        else:
            reporter.result()  # the watchdog tripped, or the row vanished under us

        done = store.get(hash_)
        if not done:
            return
        done.state = STATE_COMPLETED
        done.local_progress = 1.0
        done.dlspeed = 0
        done.completion_on = int(time.time())
        done.content_path = _content_path(done, done.files)
        done.save_path = _save_path(done.category)
        done.error = ""
        store.upsert(done)
        elapsed = max(time.time() - started, 1)
        store.add_event(
            done.hash, done.name, done.category, "downloaded",
            detail=f"{len(done.files)} files in {int(elapsed)}s "
                   f"({total / elapsed / (1 << 20):.0f} MiB/s), ready for import",
            size=total,
        )
        log.info("Completed local download: %s", done.name)
        _attempts.pop(hash_, None)
        _retry_after.pop(hash_, None)
    except Exception as exc:  # noqa: BLE001
        # TaskGroup wraps failures in an ExceptionGroup; surface the first cause.
        if isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        cur = store.get(hash_)
        if cur:
            n = _attempts.get(hash_, 0) + 1
            _attempts[hash_] = n
            if n < _TORRENT_RETRY_LIMIT:
                # Back to the cloud state so the next sync re-triggers the pull;
                # already-downloaded bytes resume via Range.
                delay = _retry_delay(n)
                if delay > 0:
                    _retry_after[hash_] = time.time() + delay
                cur.state = STATE_CLOUD
                cur.dlspeed = 0
                store.upsert(cur)
                log.warning("Local download of %s failed (round %d/%d) — retrying in %ds: %s",
                            cur.name, n, _TORRENT_RETRY_LIMIT, int(delay), exc)
            else:
                cur.state = STATE_ERROR
                cur.error = f"local download failed: {exc}"
                store.upsert(cur)
                store.add_event(cur.hash, cur.name, cur.category, "error",
                                detail=cur.error, size=cur.size)
                log.exception("Local download failed for %s: %s", hash_, exc)
    finally:
        _downloading.discard(hash_)


def _files_local(t: Torrent) -> bool:
    """True if every known file is fully present under download_dir."""
    if not t.files:
        return False
    for f in t.files:
        try:
            dest = _file_dest(t, f)
            if os.path.getsize(dest) != int(f.get("size") or 0):
                return False
        except (OSError, ValueError):
            return False
    return True


def repair_paths() -> None:
    """Fix entries recorded while SAVE_PATH was misconfigured (e.g. empty).

    An empty SAVE_PATH used to make save_path/content_path relative
    ("radarr/…"), which Sonarr/Radarr can never import from. Re-derive them
    from the current settings so existing torrents recover after upgrade.
    """
    for t in store.all():
        bad_save = bool(t.save_path) and not os.path.isabs(t.save_path)
        bad_content = bool(t.content_path) and not os.path.isabs(t.content_path)
        if not (bad_save or bad_content):
            continue
        t.save_path = _save_path(t.category)
        if t.content_path:
            t.content_path = _content_path(t, t.files)
        # If the misconfiguration also sent the files somewhere else (e.g. an
        # empty DOWNLOAD_DIR wrote into the container FS), pull them again.
        if t.state == STATE_COMPLETED and not _files_local(t):
            t.state = STATE_CLOUD
            t.completion_on = 0
            t.local_progress = 0.0
            log.info("Files for %s missing under %s — re-downloading", t.name, _category_dir(t.category))
        store.upsert(t)
        log.info("Repaired relative paths for %s -> %s", t.name, t.save_path)
    for name, path in store.categories().items():
        if path and not os.path.isabs(path):
            store.set_category(name, os.path.join(settings.save_path_base, name))
            log.info("Repaired relative save path for category %s", name)


async def _cleanup_cloud(t: Torrent) -> None:
    """Delete the TorBox cloud copy of a locally-completed torrent.

    Frees the account's active-torrent slots; the local files (and our
    tracking entry, so Sonarr/Radarr can still import) are kept.
    """
    try:
        await client.control(t.torbox_id, "delete")
    except Exception as exc:  # noqa: BLE001
        log.warning("TorBox cleanup delete failed for %s: %s", t.name, exc)
        return
    hours = runtime.get("torbox_cleanup_hours")
    # Re-read after the await: torrents/delete may have removed the row while
    # the cloud delete was in flight, and upserting our copy would resurrect it.
    t = store.get(t.hash)
    if t is None:
        return
    t.torbox_id = None
    store.upsert(t)
    store.add_event(t.hash, t.name, t.category, "cloud_removed",
                    detail=f"cloud copy removed after {hours:g}h "
                           "(local files kept)", size=t.size)
    log.info("Removed TorBox cloud copy of %s (completed >%gh ago)", t.name, hours)


def resume_interrupted() -> None:
    """Re-queue torrents that were mid-pull when the container stopped.

    Their DB state is still 'downloading', which the sync loop treats as
    already-running — without this they would hang at their last percentage
    forever after a restart.
    """
    for t in store.all():
        if t.state == STATE_DOWNLOADING:
            t.state = STATE_CLOUD
            t.dlspeed = 0
            store.upsert(t)
            log.info("Resuming interrupted local download after restart: %s", t.name)


def _needs_cloud_cleanup(t: Torrent, now: int, cleanup_after: float) -> bool:
    return bool(t.state == STATE_COMPLETED and cleanup_after > 0 and t.torbox_id is not None
                and t.completion_on and now - t.completion_on >= cleanup_after)


async def sync_once() -> None:
    tracked = store.all()
    now = int(time.time())
    cleanup_after = runtime.get("torbox_cleanup_hours") * 3600

    # Age-based cloud cleanup deletes by id, so it needs no listing — run it
    # before deciding whether the poll itself is worth making.
    for t in tracked:
        if _needs_cloud_cleanup(t, now, cleanup_after):
            await _cleanup_cloud(t)

    # Completed/errored rows are kept on purpose (Sonarr/Radarr still have to
    # import them), but they are never matched against a mylist entry — after
    # _cleanup_cloud() they don't even have a torbox_id any more. Polling for
    # those alone means an uncached listing of the whole TorBox account every
    # POLL_INTERVAL, forever, with nothing to do.
    tracked = [t for t in tracked if t.state not in (STATE_COMPLETED, STATE_ERROR)]
    if not tracked:
        return

    try:
        entries = await client.my_list()
    except Exception as exc:  # noqa: BLE001
        log.warning("TorBox mylist failed: %s", exc)
        # Feed the burst alert directly: a dead API/key only ever logs warnings,
        # but a stretch of failed polls is exactly what the user wants to hear about.
        notify.record_error(f"TorBox mylist failed: {exc}")
        return

    by_id = {e.get("id"): e for e in entries if e.get("id") is not None}
    by_hash = {str(e.get("hash", "")).lower(): e for e in entries}

    for stale in tracked:
        # Re-read: the mylist call above is a network round trip, and on a large
        # account it is not quick. A download task can finish, fail or be put
        # back to the cloud state inside that window, and writing the snapshot
        # we took beforehand back over the row would undo it — leaving the
        # torrent in 'downloading' with no task behind it, which the start gate
        # below then refuses to restart. That wedges it until the next restart.
        t = store.get(stale.hash)
        if t is None or t.state in (STATE_COMPLETED, STATE_ERROR):
            continue

        if t.state == STATE_DOWNLOADING and t.hash not in _downloading:
            # No task owns this row. _download_torrent only leaves _downloading
            # once it has written a final state, so this means the task is gone:
            # killed mid-flight, or lost to the overwrite described above by an
            # older build. resume_interrupted() only runs at startup, so recover
            # it here instead of waiting for one.
            log.info("Requeuing %s — marked downloading with no task behind it", t.name)
            t.state = STATE_CLOUD
            t.dlspeed = 0
            store.upsert(t)

        entry = None
        if t.torbox_id is not None:
            entry = by_id.get(t.torbox_id)
        if entry is None:
            entry = by_hash.get(t.hash.lower())
        if entry is None:
            continue  # not visible yet (still queued) or removed on TorBox side

        _apply_cloud_update(t, entry)
        if t.state == STATE_ERROR:
            store.upsert(t)
            store.add_event(t.hash, t.name, t.category, "error", detail=t.error, size=t.size)
            continue

        ready = bool(entry.get("download_finished")) and bool(entry.get("download_present", True))

        if ready and not t.files:
            # TorBox calls the cloud copy finished but hands us nothing to
            # fetch. _download_torrent bails before it even logs, so this spins
            # silently on every poll — one torrent sat like this for 45 days.
            # Give TorBox a grace period to populate the list, then fail so the
            # *arr app is free to grab a different release.
            waited = time.time() - _fileless_since.setdefault(t.hash, time.time())
            if settings.fileless_timeout > 0 and waited >= settings.fileless_timeout:
                t.state = STATE_ERROR
                t.error = (f"TorBox reported the download finished but returned no "
                           f"file list within {int(waited / 60)} minutes")
                store.upsert(t)
                store.add_event(t.hash, t.name, t.category, "error",
                                detail=t.error, size=t.size)
                log.warning("No file list for %s after %ds — reporting it failed",
                            t.name, int(waited))
                _fileless_since.pop(t.hash, None)
            else:
                store.upsert(t)
            continue
        _fileless_since.pop(t.hash, None)

        slots_full = (settings.max_parallel_torrents > 0
                      and len(_downloading) >= settings.max_parallel_torrents)
        backing_off = time.time() < _retry_after.get(t.hash, 0)
        if (ready and t.hash not in _downloading and t.state != STATE_DOWNLOADING
                and not slots_full and not backing_off):
            t.state = STATE_DOWNLOADING
            t.save_path = _save_path(t.category)
            store.upsert(t)
            _downloading.add(t.hash)
            _spawn(_download_torrent(t.hash))
        else:
            if t.state == STATE_QUEUED:
                t.state = STATE_CLOUD
            store.upsert(t)


async def run() -> None:
    global _last_tick
    log.info("Worker started (poll every %ds)", settings.poll_interval)
    while True:
        try:
            await sync_once()
            await notify.maybe_notify_error_burst()
        except Exception as exc:  # noqa: BLE001
            log.exception("sync loop error: %s", exc)
        _last_tick = time.time()
        await asyncio.sleep(settings.poll_interval)


# --------------------------------------------------------------------------- #
# housekeeping: subscription status + age-based cloud cleanup
# --------------------------------------------------------------------------- #
_HOUSEKEEP_INTERVAL = 1800  # seconds between subscription/age-cleanup passes

# Latest snapshot from /user/me; served by the web UI header.
_subscription: dict = {}


def subscription_status() -> dict | None:
    return dict(_subscription) if _subscription else None


def _parse_time(value) -> float | None:
    """Epoch seconds from a TorBox timestamp (ISO 8601 string or epoch number)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def refresh_subscription() -> None:
    try:
        me = await client.user_me()
    except Exception as exc:  # noqa: BLE001
        log.warning("TorBox user info fetch failed: %s", exc)
        return
    expires_raw = str(me.get("premium_expires_at") or "")
    expires_ts = _parse_time(expires_raw)
    days_left = (expires_ts - time.time()) / 86400 if expires_ts is not None else None
    _subscription.clear()
    _subscription.update({
        "plan": me.get("plan"),
        "expires_at": expires_raw or None,
        "days_left": round(days_left, 1) if days_left is not None else None,
        "checked_at": int(time.time()),
    })
    await notify.maybe_notify_subscription(days_left, expires_raw)


async def cleanup_aged_cloud() -> None:
    """Delete TorBox items older than ``cloud_max_age_days`` — tracked or not —
    sparing anything whose local pull is still running."""
    days = runtime.get("cloud_max_age_days")
    if days <= 0:
        return
    try:
        entries = await client.my_list()
    except Exception as exc:  # noqa: BLE001
        log.warning("Age cleanup: mylist failed: %s", exc)
        return
    cutoff = time.time() - days * 86400
    for entry in entries:
        tid = entry.get("id")
        if tid is None:
            continue
        created = _parse_time(entry.get("created_at") or entry.get("added_at") or entry.get("added"))
        if created is None or created > cutoff:
            continue
        hash_ = str(entry.get("hash") or "").lower()
        t = store.get(hash_) if hash_ else None
        if t and (t.hash in _downloading or t.state == STATE_DOWNLOADING):
            continue  # let the local pull finish; the next pass will catch it
        name = entry.get("name") or (t.name if t else hash_) or str(tid)
        try:
            await client.control(tid, "delete")
        except Exception as exc:  # noqa: BLE001
            log.warning("Age cleanup delete failed for %s: %s", name, exc)
            continue
        if t:
            t.torbox_id = None
            if t.state != STATE_COMPLETED:
                t.state = STATE_ERROR
                t.error = f"removed from TorBox by age cleanup (older than {days:g} days)"
            store.upsert(t)
        store.add_event(hash_ or str(tid), name, t.category if t else "", "cloud_removed",
                        detail=f"age cleanup: older than {days:g} days on TorBox",
                        size=int(entry.get("size") or 0))
        log.info("Age cleanup: removed %s from TorBox (older than %gd)", name, days)


async def housekeeping() -> None:
    """Slow loop for work that doesn't belong in the per-poll sync."""
    log.info("Housekeeping started (every %d min)", _HOUSEKEEP_INTERVAL // 60)
    while True:
        try:
            await refresh_subscription()
            await cleanup_aged_cloud()
        except Exception as exc:  # noqa: BLE001
            log.exception("housekeeping error: %s", exc)
        await asyncio.sleep(_HOUSEKEEP_INTERVAL)
