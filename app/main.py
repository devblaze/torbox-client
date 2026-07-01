"""FastAPI application entrypoint.

Serves the qBittorrent-compatible API and runs the TorBox sync worker in the
background. Point Sonarr/Radarr at this service as a qBittorrent download client.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from . import worker
from .config import settings
from .qbit_api import api, public, require_auth
from .torbox_client import client

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("main")

app = FastAPI(title="torbox-sonarr-downloader", docs_url=None, redoc_url=None)

# Public routes (login) + guarded routes (everything else needs the SID cookie).
app.include_router(public)
app.include_router(api, dependencies=[Depends(require_auth)])

_worker_task: asyncio.Task | None = None


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
