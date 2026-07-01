"""A subset of the qBittorrent Web API v2, enough for Sonarr/Radarr.

Sonarr/Radarr add this service as a "qBittorrent" download client. They log in,
poll ``/torrents/info``, push magnets/torrent files to ``/torrents/add``, read
``/torrents/files`` and, after import, call ``/torrents/delete``. We translate
all of that to TorBox operations and our local download state.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import uuid

import httpx
from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from . import bencode
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

log = logging.getLogger("qbit")

# Active login sessions (SID cookie values). Cleared on restart; the *arr apps
# simply log in again after a 403.
_SESSIONS: set[str] = set()

public = APIRouter()   # login, no auth required
api = APIRouter()      # everything else, auth required


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
async def require_auth(SID: str | None = Cookie(default=None)) -> None:
    if SID and SID in _SESSIONS:
        return
    # Match qBittorrent: unauthenticated calls get 403.
    from fastapi import HTTPException
    raise HTTPException(status_code=403, detail="Forbidden")


@public.post("/api/v2/auth/login")
async def login(request: Request) -> Response:
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if username == settings.qbit_user and password == settings.qbit_pass:
        sid = uuid.uuid4().hex
        _SESSIONS.add(sid)
        resp = PlainTextResponse("Ok.")
        resp.set_cookie("SID", sid, httponly=True, samesite="lax", path="/")
        return resp
    return PlainTextResponse("Fails.", status_code=200)


@api.post("/api/v2/auth/logout")
async def logout(SID: str | None = Cookie(default=None)) -> Response:
    if SID:
        _SESSIONS.discard(SID)
    return PlainTextResponse("Ok.")


# --------------------------------------------------------------------------- #
# app info
# --------------------------------------------------------------------------- #
@api.get("/api/v2/app/version")
async def app_version() -> Response:
    return PlainTextResponse(settings.qbit_app_version)


@api.get("/api/v2/app/webapiVersion")
async def webapi_version() -> Response:
    return PlainTextResponse(settings.qbit_webapi_version)


@api.get("/api/v2/app/preferences")
async def preferences() -> Response:
    return JSONResponse({
        "save_path": settings.save_path_base,
        "temp_path_enabled": False,
        "temp_path": settings.save_path_base,
        "queueing_enabled": False,
        "dht": True,
        "max_ratio_enabled": False,
        "max_ratio": -1,
        "max_ratio_act": 0,
        "max_seeding_time_enabled": False,
        "max_seeding_time": -1,
        "create_subfolder_enabled": True,
        "start_paused_enabled": False,
        "auto_delete_mode": 0,
        "listen_port": settings.listen_port,
    })


@api.get("/api/v2/app/buildInfo")
async def build_info() -> Response:
    return JSONResponse({
        "bitness": 64, "boost": "1.83.0", "libtorrent": "1.2.19",
        "openssl": "3.0.0", "qt": "5.15.2", "zlib": "1.3",
    })


# --------------------------------------------------------------------------- #
# transfer info
# --------------------------------------------------------------------------- #
@api.get("/api/v2/transfer/info")
async def transfer_info() -> Response:
    dl = sum(t.dlspeed for t in store.all())
    return JSONResponse({
        "dl_info_speed": dl, "dl_info_data": 0,
        "up_info_speed": 0, "up_info_data": 0,
        "dl_rate_limit": 0, "up_rate_limit": 0,
        "dht_nodes": 0, "connection_status": "connected",
    })


# --------------------------------------------------------------------------- #
# categories
# --------------------------------------------------------------------------- #
@api.get("/api/v2/torrents/categories")
async def categories() -> Response:
    cats = store.categories()
    return JSONResponse({
        name: {"name": name, "savePath": path or os.path.join(settings.save_path_base, name)}
        for name, path in cats.items()
    })


@api.post("/api/v2/torrents/createCategory")
async def create_category(request: Request) -> Response:
    form = await request.form()
    name = form.get("category", "")
    save_path = form.get("savePath", "") or os.path.join(settings.save_path_base, name)
    if name:
        store.set_category(name, save_path)
    return PlainTextResponse("Ok.")


@api.post("/api/v2/torrents/editCategory")
async def edit_category(request: Request) -> Response:
    return await create_category(request)


@api.post("/api/v2/torrents/removeCategories")
async def remove_categories(request: Request) -> Response:
    form = await request.form()
    for name in (form.get("categories", "") or "").split("\n"):
        if name.strip():
            store.remove_category(name.strip())
    return PlainTextResponse("Ok.")


@api.post("/api/v2/torrents/setCategory")
async def set_category(request: Request) -> Response:
    form = await request.form()
    category = form.get("category", "")
    hashes = (form.get("hashes", "") or "").split("|")
    for h in hashes:
        t = store.get(h.strip())
        if t:
            t.category = category
            store.upsert(t)
    return PlainTextResponse("Ok.")


# --------------------------------------------------------------------------- #
# torrent listing
# --------------------------------------------------------------------------- #
def _qbit_state(t: Torrent) -> str:
    if t.state == STATE_ERROR:
        return "error"
    if t.state == STATE_COMPLETED:
        return "pausedUP"  # finished + safe to import/remove
    if t.state == STATE_DOWNLOADING:
        return "downloading"
    if t.state == STATE_QUEUED:
        return "metaDL"
    # cloud phase
    return "downloading" if t.dlspeed > 0 else "stalledDL"


def _to_qbit(t: Torrent) -> dict:
    progress = t.progress
    amount_left = max(int(t.size * (1 - progress)), 0)
    completed = t.size - amount_left
    eta = int(amount_left / t.dlspeed) if t.dlspeed > 0 and amount_left > 0 else 8640000
    save_path = t.save_path or (
        os.path.join(settings.save_path_base, t.category) if t.category else settings.save_path_base
    )
    content_path = t.content_path or os.path.join(save_path, t.name)
    now = int(time.time())
    return {
        "hash": t.hash,
        "name": t.name,
        "size": t.size,
        "total_size": t.size,
        "progress": progress,
        "dlspeed": t.dlspeed,
        "upspeed": 0,
        "priority": 0,
        "num_seeds": 0,
        "num_complete": 0,
        "num_leechs": 0,
        "num_incomplete": 0,
        "ratio": 0.0,
        "ratio_limit": -1,
        "eta": eta,
        "state": _qbit_state(t),
        "category": t.category,
        "tags": "",
        "super_seeding": False,
        "force_start": False,
        "save_path": save_path,
        "content_path": content_path,
        "download_path": save_path,
        "added_on": t.added_on or now,
        "completion_on": t.completion_on or 0,
        "completed": completed,
        "amount_left": amount_left,
        "time_active": max(now - (t.added_on or now), 0),
        "seeding_time": 0,
        "seeding_time_limit": -1,
        "last_activity": t.last_update or now,
        "auto_tmm": False,
        "availability": 1.0 if progress >= 1 else -1.0,
    }


@api.get("/api/v2/torrents/info")
async def torrents_info(category: str | None = None, hashes: str | None = None) -> Response:
    items = store.all()
    if category is not None:
        items = [t for t in items if t.category == category]
    if hashes:
        wanted = {h.lower() for h in hashes.split("|")}
        items = [t for t in items if t.hash in wanted]
    return JSONResponse([_to_qbit(t) for t in items])


@api.get("/api/v2/torrents/properties")
async def torrent_properties(hash: str) -> Response:
    t = store.get(hash)
    if not t:
        return JSONResponse({}, status_code=404)
    q = _to_qbit(t)
    return JSONResponse({
        "save_path": q["save_path"],
        "creation_date": q["added_on"],
        "piece_size": 0,
        "pieces_num": 0,
        "pieces_have": 0,
        "total_wasted": 0,
        "total_downloaded": q["completed"],
        "total_uploaded": 0,
        "dl_speed": t.dlspeed,
        "up_speed": 0,
        "total_size": t.size,
        "addition_date": q["added_on"],
        "completion_date": t.completion_on or -1,
        "seeding_time": 0,
        "eta": q["eta"],
    })


@api.get("/api/v2/torrents/files")
async def torrent_files(hash: str) -> Response:
    t = store.get(hash)
    if not t:
        return JSONResponse([])
    done = t.state == STATE_COMPLETED
    out = []
    for idx, f in enumerate(t.files):
        out.append({
            "index": idx,
            "name": f.get("name", ""),
            "size": int(f.get("size") or 0),
            "progress": 1.0 if done else t.local_progress,
            "priority": 1,
            "is_seed": done,
            "piece_range": [0, 0],
            "availability": 1.0 if done else -1.0,
        })
    return JSONResponse(out)


# --------------------------------------------------------------------------- #
# add / delete
# --------------------------------------------------------------------------- #
async def _fetch_torrent_url(url: str) -> bytes:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content


async def _add_magnet(magnet: str, category: str) -> None:
    infohash = bencode.infohash_from_magnet(magnet)
    data = await client.add_magnet(magnet)
    _record_added(data, infohash, category, name_hint=_magnet_name(magnet))


async def _add_torrent_bytes(content: bytes, category: str) -> None:
    infohash = bencode.infohash_from_torrent(content)
    name = bencode.torrent_name(content)
    data = await client.add_torrent_file(content)
    _record_added(data, infohash, category, name_hint=name)


def _magnet_name(magnet: str) -> str | None:
    from urllib.parse import parse_qs, urlparse
    dn = parse_qs(urlparse(magnet).query).get("dn")
    return dn[0] if dn else None


def _record_added(data: dict, infohash: str | None, category: str, name_hint: str | None) -> None:
    tb_hash = str(data.get("hash", "")).lower() or None
    hash_ = (infohash or tb_hash)
    if not hash_:
        log.warning("Could not determine infohash for added torrent: %s", data)
        return
    existing = store.get(hash_)
    t = existing or Torrent(hash=hash_, name=name_hint or data.get("name") or hash_)
    t.torbox_id = data.get("torrent_id", t.torbox_id)
    t.category = category or t.category
    if name_hint and (not t.name or t.name == t.hash):
        t.name = name_hint
    if not t.added_on:
        t.added_on = int(time.time())
    t.save_path = os.path.join(settings.save_path_base, t.category) if t.category else settings.save_path_base
    if t.state in ("", STATE_QUEUED) or existing is None:
        t.state = STATE_QUEUED
    store.upsert(t)
    log.info("Queued %s (torbox_id=%s category=%s)", t.name, t.torbox_id, t.category)


@api.post("/api/v2/torrents/add")
async def torrents_add(request: Request) -> Response:
    form = await request.form()
    category = form.get("category", "") or ""
    if category:
        store.set_category(category, os.path.join(settings.save_path_base, category))

    added = 0
    # URLs / magnets (newline-separated).
    urls_raw = form.get("urls", "") or ""
    for line in urls_raw.splitlines():
        url = line.strip()
        if not url:
            continue
        try:
            if url.startswith("magnet:"):
                await _add_magnet(url, category)
            elif url.startswith("http://") or url.startswith("https://"):
                content = await _fetch_torrent_url(url)
                await _add_torrent_bytes(content, category)
            else:
                continue
            added += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to add url %s: %s", url, exc)

    # Uploaded .torrent files (field name "torrents").
    for upload in form.getlist("torrents"):
        try:
            content = await upload.read()  # UploadFile
            await _add_torrent_bytes(content, category)
            added += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to add uploaded torrent: %s", exc)

    if added == 0:
        return PlainTextResponse("Fails.", status_code=200)
    return PlainTextResponse("Ok.")


def _delete_local(t: Torrent) -> None:
    """Remove the downloaded content (file or root folder) from local disk."""
    from .worker import _category_dir  # local import to avoid a cycle at import time
    base = _category_dir(t.category)
    roots = {f.get("name", "").split("/", 1)[0] for f in t.files if f.get("name")}
    for root in roots:
        if not root:
            continue
        path = os.path.join(base, root)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not delete %s: %s", path, exc)


@api.post("/api/v2/torrents/delete")
async def torrents_delete(request: Request) -> Response:
    form = await request.form()
    hashes = form.get("hashes", "") or ""
    delete_files = (form.get("deleteFiles", "false") or "false").lower() in {"true", "1"}
    targets = store.all() if hashes == "all" else [store.get(h) for h in hashes.split("|")]

    for t in targets:
        if not t:
            continue
        if delete_files:
            _delete_local(t)
        if settings.delete_from_torbox_on_remove and t.torbox_id:
            try:
                await client.control(t.torbox_id, "delete")
            except Exception as exc:  # noqa: BLE001
                log.warning("TorBox delete failed for %s: %s", t.torbox_id, exc)
        store.delete(t.hash)
        log.info("Removed %s (deleteFiles=%s)", t.name, delete_files)
    return PlainTextResponse("Ok.")


# --------------------------------------------------------------------------- #
# no-op endpoints Sonarr/Radarr may call — accept and ignore.
# --------------------------------------------------------------------------- #
_NOOP_PATHS = [
    "pause", "resume", "recheck", "reannounce", "setForceStart",
    "setSuperSeeding", "topPrio", "bottomPrio", "setShareLimits",
    "setAutoManagement", "setLocation", "rename", "addTags", "removeTags",
    "toggleSequentialDownload",
]


def _register_noops() -> None:
    for name in _NOOP_PATHS:
        async def _noop() -> Response:  # noqa: ANN202
            return PlainTextResponse("Ok.")
        api.add_api_route(f"/api/v2/torrents/{name}", _noop, methods=["POST"])


_register_noops()
