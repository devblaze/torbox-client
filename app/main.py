"""FastAPI application entrypoint.

Serves the qBittorrent-compatible API and runs the TorBox sync worker in the
background. Point Sonarr/Radarr at this service as a qBittorrent download client.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import logbuffer, worker
from .config import settings
from .qbit_api import api, public, require_auth
from .torbox_client import client
from .webui import ui_api, ui_public

# Root at DEBUG so the web UI's log buffer sees everything; the console handler
# keeps honouring LOG_LEVEL so `docker logs` stays as quiet as before.
_console = logging.StreamHandler()
_console.setLevel(getattr(logging, settings.log_level, logging.INFO))
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
logging.basicConfig(level=logging.DEBUG, handlers=[_console, logbuffer.handler()])
logging.getLogger("httpcore").setLevel(logging.INFO)  # per-chunk DEBUG spam
logging.getLogger("python_multipart").setLevel(logging.INFO)  # per-form-field DEBUG spam
log = logging.getLogger("main")

app = FastAPI(title="torbox-sonarr-downloader", docs_url=None, redoc_url=None)

# Public routes (login) + guarded routes (everything else needs the SID cookie).
app.include_router(public)
app.include_router(api, dependencies=[Depends(require_auth)])
# Human-facing dashboard: the page is public, its data endpoint shares API auth.
app.include_router(ui_public)
app.include_router(ui_api, dependencies=[Depends(require_auth)])

_worker_task: asyncio.Task | None = None


@app.middleware("http")
async def _log_api_requests(request: Request, call_next):
    """Trace what Sonarr/Radarr call, visible in the web UI's debug log."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        log.debug("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response


async def _validate_key() -> None:
    """Log whether the TorBox key works, without blocking startup."""
    if not settings.torbox_api_key:
        log.error("TORBOX_API_KEY is not set — the service will not work until you set it.")
        return
    ok = await client.validate_key()
    log.info("TorBox API key %s", "validated" if ok else "REJECTED (check TORBOX_API_KEY)")


@app.on_event("startup")
async def _startup() -> None:
    # Fire-and-forget so a slow/unreachable TorBox API never blocks the server
    # from accepting Sonarr/Radarr connections (or health checks).
    asyncio.create_task(_validate_key())
    worker.repair_paths()
    global _worker_task
    _worker_task = asyncio.create_task(worker.run())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _worker_task:
        _worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _worker_task
    await client.close()


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
