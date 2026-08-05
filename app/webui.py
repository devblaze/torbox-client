"""Minimal dashboard for humans, served at ``/``.

The qBittorrent-compatible API (qbit_api.py) is what Sonarr/Radarr talk to;
this module exposes the same underlying state in a friendlier shape for the
bundled single-page UI: the two-phase lifecycle (cloud download on TorBox,
then local pull) is shown explicitly instead of being squashed into qBittorrent
state strings. Auth reuses the same SID cookie as the API, so the UI login is
the QBIT_USER / QBIT_PASS pair.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from . import logbuffer, notify, runtime, worker
from .store import STATE_COMPLETED, STATE_DOWNLOADING, STATE_ERROR, store

# Mounted with the same require_auth dependency as the qBittorrent API.
ui_api = APIRouter()
# The page itself is public; it shows a login form until the SID cookie works.
ui_public = APIRouter()

_INDEX = os.path.join(os.path.dirname(__file__), "static", "index.html")


@ui_public.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_INDEX, media_type="text/html")


@ui_api.get("/ui/api/state")
async def ui_state() -> JSONResponse:
    torrents = store.all()
    now = int(time.time())
    items = [
        {
            "hash": t.hash,
            "name": t.name,
            "category": t.category,
            "state": t.state,
            "error": t.error,
            "size": t.size,
            "progress": t.progress,
            "cloud_progress": t.cloud_progress,
            "local_progress": t.local_progress,
            "dlspeed": t.dlspeed,
            "added_on": t.added_on,
            "completion_on": t.completion_on,
            "save_path": t.save_path,
            "file_count": len(t.files),
        }
        for t in torrents
    ]
    items.sort(key=lambda i: i["added_on"], reverse=True)
    active = [i for i in items if i["state"] not in (STATE_COMPLETED, STATE_ERROR)]
    # warn_days is attached at serve time so a settings change shows its effect
    # immediately instead of after the next housekeeping pass.
    subscription = worker.subscription_status()
    if subscription is not None:
        subscription["warn_days"] = runtime.get("sub_warn_days")
    return JSONResponse({
        "now": now,
        "subscription": subscription,
        "torrents": items,
        "summary": {
            "total": len(items),
            "active": len(active),
            "completed": sum(1 for i in items if i["state"] == STATE_COMPLETED),
            "errored": sum(1 for i in items if i["state"] == STATE_ERROR),
            "pulling": sum(1 for i in items if i["state"] == STATE_DOWNLOADING),
            "dlspeed": sum(i["dlspeed"] for i in active),
        },
    })


@ui_api.get("/ui/api/history")
async def ui_history(limit: int = 200) -> JSONResponse:
    return JSONResponse({"events": store.history(min(max(limit, 1), 1000))})


@ui_api.get("/ui/api/logs")
async def ui_logs(limit: int = 500, level: str = "DEBUG") -> JSONResponse:
    return JSONResponse({"logs": logbuffer.entries(min(max(limit, 1), 2000), level)})


@ui_api.get("/ui/api/settings")
async def ui_settings() -> JSONResponse:
    return JSONResponse({"settings": runtime.as_dict(), "pushover_ready": notify.configured()})


@ui_api.post("/ui/api/settings")
async def ui_settings_save(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)
    try:
        runtime.update(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"settings": runtime.as_dict(), "pushover_ready": notify.configured()})


@ui_api.post("/ui/api/notify-test")
async def ui_notify_test() -> JSONResponse:
    ok, detail = await notify.send(
        "torbox-client test",
        "Test notification — Pushover is wired up correctly.")
    return JSONResponse({"ok": ok, "detail": detail})
