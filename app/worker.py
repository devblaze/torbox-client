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
from typing import Optional

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
_file_semaphore = asyncio.Semaphore(settings.max_parallel_downloads)
# Failed local-pull attempts per torrent; reset on success, capped below.
_attempts: dict[str, int] = {}
_TORRENT_RETRY_LIMIT = 3

_FAILED_STATES = {"failed", "error", "cberror", "uploaderror"}


class _RateLimiter:
    """Leaky-bucket limiter shared by all file streams (aggregate cap)."""

    def __init__(self, rate_bytes_per_s: float):
        self.rate = rate_bytes_per_s
        self._next_free = 0.0
        self._lock = asyncio.Lock()

    async def throttle(self, nbytes: int) -> None:
        if self.rate <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._next_free = max(self._next_free, now) + nbytes / self.rate
            wait = self._next_free - now
        if wait > 0:
            await asyncio.sleep(wait)


_rate = _RateLimiter(settings.max_download_speed * (1 << 20))


def _category_dir(category: str) -> str:
    """Local dir this container writes into."""
    return os.path.join(settings.download_dir, category) if category else settings.download_dir


def _save_path(category: str) -> str:
    """Path the *arr containers see (may differ from _category_dir via SAVE_PATH)."""
    return os.path.join(settings.save_path_base, category) if category else settings.save_path_base


def _content_path(t: Torrent, files: list[dict]) -> str:
    """Root file/folder path as the *arr apps should see it."""
    base = _save_path(t.category)
    names = [f.get("name", "") for f in files if f.get("name")]
    if not names:
        return os.path.join(base, t.name)
    # Common first path segment == the torrent's root folder (or a single file).
    tops = {n.replace("\\", "/").split("/", 1)[0] for n in names}
    if len(tops) == 1:
        return os.path.join(base, next(iter(tops)))
    return base


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
    dest = os.path.join(_category_dir(t.category), rel)
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
            async with _file_semaphore:
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


async def _download_torrent(hash_: str) -> None:
    t = store.get(hash_)
    if not t or not t.torbox_id or not t.files:
        _downloading.discard(hash_)
        return
    total = sum(int(f.get("size") or 0) for f in t.files) or t.size or 1
    progress = {"done": 0}
    started = time.time()
    log.info("Downloading %s (%d files, %.2f GiB)", t.name, len(t.files), total / (1 << 30))
    try:
        # A light periodic progress writer while files stream in.
        async def _report() -> None:
            last_done = progress["done"]
            while True:
                await asyncio.sleep(3)
                cur = store.get(hash_)
                if not cur:
                    return
                cur.state = STATE_DOWNLOADING
                cur.local_progress = min(progress["done"] / total, 1.0)
                # Windowed speed: shows 0 during a stall instead of a decaying average.
                cur.dlspeed = max(int((progress["done"] - last_done) / 3), 0)
                last_done = progress["done"]
                store.upsert(cur)

        reporter = asyncio.create_task(_report())
        try:
            await asyncio.gather(*(_download_file(t, f, progress) for f in t.files))
        finally:
            reporter.cancel()

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
    except Exception as exc:  # noqa: BLE001
        cur = store.get(hash_)
        if cur:
            n = _attempts.get(hash_, 0) + 1
            _attempts[hash_] = n
            if n < _TORRENT_RETRY_LIMIT:
                # Back to the cloud state so the next sync re-triggers the pull;
                # already-downloaded bytes resume via Range.
                cur.state = STATE_CLOUD
                cur.dlspeed = 0
                store.upsert(cur)
                log.warning("Local download of %s failed (round %d/%d) — will retry: %s",
                            cur.name, n, _TORRENT_RETRY_LIMIT, exc)
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
    base = _category_dir(t.category)
    for f in t.files:
        dest = os.path.join(base, f.get("name", ""))
        try:
            if os.path.getsize(dest) != int(f.get("size") or 0):
                return False
        except OSError:
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
    t.torbox_id = None
    store.upsert(t)
    store.add_event(t.hash, t.name, t.category, "cloud_removed",
                    detail=f"cloud copy removed after {settings.torbox_cleanup_hours:g}h "
                           "(local files kept)", size=t.size)
    log.info("Removed TorBox cloud copy of %s (completed >%gh ago)",
             t.name, settings.torbox_cleanup_hours)


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


async def sync_once() -> None:
    tracked = store.all()
    if not tracked:
        return
    try:
        entries = await client.my_list()
    except Exception as exc:  # noqa: BLE001
        log.warning("TorBox mylist failed: %s", exc)
        return

    by_id = {e.get("id"): e for e in entries if e.get("id") is not None}
    by_hash = {str(e.get("hash", "")).lower(): e for e in entries}
    now = int(time.time())
    cleanup_after = settings.torbox_cleanup_hours * 3600

    for t in tracked:
        if (t.state == STATE_COMPLETED and cleanup_after > 0 and t.torbox_id is not None
                and t.completion_on and now - t.completion_on >= cleanup_after):
            await _cleanup_cloud(t)
        if t.state in (STATE_COMPLETED, STATE_ERROR):
            continue
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
        slots_full = (settings.max_parallel_torrents > 0
                      and len(_downloading) >= settings.max_parallel_torrents)
        if ready and t.hash not in _downloading and t.state != STATE_DOWNLOADING and not slots_full:
            t.state = STATE_DOWNLOADING
            t.save_path = _save_path(t.category)
            store.upsert(t)
            _downloading.add(t.hash)
            asyncio.create_task(_download_torrent(t.hash))
        else:
            if t.state == STATE_QUEUED:
                t.state = STATE_CLOUD
            store.upsert(t)


async def run() -> None:
    log.info("Worker started (poll every %ds)", settings.poll_interval)
    while True:
        try:
            await sync_once()
        except Exception as exc:  # noqa: BLE001
            log.exception("sync loop error: %s", exc)
        await asyncio.sleep(settings.poll_interval)
