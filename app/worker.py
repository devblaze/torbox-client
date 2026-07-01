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

_FAILED_STATES = {"failed", "error", "cberror", "uploaderror"}


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
    """Download a single TorBox file to local disk, resuming if partial."""
    file_id = file["id"]
    rel = file["name"]
    expected = int(file.get("size") or 0)
    dest = os.path.join(_category_dir(t.category), rel)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

    existing = os.path.getsize(dest) if os.path.exists(dest) else 0
    if expected and existing == expected:
        progress["done"] += expected
        return
    if existing > expected:  # corrupt/stale — start over
        existing = 0

    async with _file_semaphore:
        url = await client.request_dl(t.torbox_id, file_id)
        headers = {"Range": f"bytes={existing}-"} if existing else None
        mode = "ab" if existing else "wb"
        got = existing
        with open(dest, mode) as fh:
            async with client.stream(url, headers=headers) as resp:
                if existing and resp.status_code == 200:
                    # Server ignored Range; rewrite from scratch.
                    fh.seek(0)
                    fh.truncate()
                    got = 0
                    progress["done"] -= existing
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(1 << 20):  # 1 MiB
                    fh.write(chunk)
                    got += len(chunk)
                    progress["done"] += len(chunk)
    if expected and got < expected:
        raise IOError(f"short read for {rel}: {got}/{expected}")


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
            while True:
                await asyncio.sleep(3)
                cur = store.get(hash_)
                if not cur:
                    return
                cur.state = STATE_DOWNLOADING
                cur.local_progress = min(progress["done"] / total, 1.0)
                elapsed = max(time.time() - started, 1)
                cur.dlspeed = int(progress["done"] / elapsed)
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
        log.info("Completed local download: %s", done.name)
    except Exception as exc:  # noqa: BLE001
        cur = store.get(hash_)
        if cur:
            cur.state = STATE_ERROR
            cur.error = f"local download failed: {exc}"
            store.upsert(cur)
        log.exception("Local download failed for %s: %s", hash_, exc)
    finally:
        _downloading.discard(hash_)


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

    for t in tracked:
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
            continue

        ready = bool(entry.get("download_finished")) and bool(entry.get("download_present", True))
        if ready and t.hash not in _downloading and t.state != STATE_DOWNLOADING:
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
