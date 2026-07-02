"""FastAPI application entrypoint.

Serves the qBittorrent-compatible API and runs the TorBox sync worker in the
background. Point Sonarr/Radarr at this service as a qBittorrent download client.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import logbuffer, worker
from .config import settings
from .qbit_api import api, public, require_auth
from .torbox_client import client
from .webui import ui_api, ui_public

# Root at DEBUG so the web UI's log buffer sees everything; the console handler
# keeps honouring LOG_LEVEL so `docker logs` stays as quiet as before. Both
# handlers get a redaction filter so the TorBox key can never reach the console
# or the HTTP-exposed log buffer.
_redact = logbuffer.RedactSecrets(settings.torbox_api_key)
_console = logging.StreamHandler()
_console.setLevel(getattr(logging, settings.log_level, logging.INFO))
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
_console.addFilter(_redact)
_buffer = logbuffer.handler()
_buffer.addFilter(_redact)
logging.basicConfig(level=logging.DEBUG, handlers=[_console, _buffer])
logging.getLogger("httpcore").setLevel(logging.INFO)  # per-chunk DEBUG spam
logging.getLogger("python_multipart").setLevel(logging.INFO)  # per-form-field DEBUG spam
log = logging.getLogger("main")

# Startup-time work that must outlive the function that scheduled it — held so
# the event loop can't garbage-collect the task before it runs.
_startup_tasks: set[asyncio.Task] = set()


async def _validate_key() -> None:
    """Log whether the TorBox key works, without blocking startup."""
    if not settings.torbox_api_key:
        log.error("TORBOX_API_KEY is not set — the service will not work until you set it.")
        return
    ok = await client.validate_key()
    log.info("TorBox API key %s", "validated" if ok else "REJECTED (check TORBOX_API_KEY)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.qbit_pass == "adminadmin":
        log.warning("QBIT_PASS is the default 'adminadmin' — set a strong password; "
                    "the dashboard and API are reachable on your LAN.")
    # Fire-and-forget so a slow/unreachable TorBox API never blocks the server
    # from accepting Sonarr/Radarr connections (or health checks).
    task = asyncio.create_task(_validate_key())
    _startup_tasks.add(task)
    task.add_done_callback(_startup_tasks.discard)
    worker.repair_paths()
    worker.resume_interrupted()
    worker_task = asyncio.create_task(worker.run())
    try:
        yield
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await worker.shutdown()  # unwind live downloads before closing the client
        await client.close()


app = FastAPI(title="torbox-sonarr-downloader", docs_url=None, redoc_url=None, lifespan=lifespan)

# Public routes (login) + guarded routes (everything else needs the SID cookie).
app.include_router(public)
app.include_router(api, dependencies=[Depends(require_auth)])
# Human-facing dashboard: the page is public, its data endpoint shares API auth.
app.include_router(ui_public)
app.include_router(ui_api, dependencies=[Depends(require_auth)])


@app.middleware("http")
async def _log_api_requests(request: Request, call_next):
    """Trace what Sonarr/Radarr call, visible in the web UI's debug log."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        log.debug("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response


@app.get("/health")
async def health() -> JSONResponse:
    # The worker updates its heartbeat every poll cycle; flag it stale if it has
    # gone quiet for far longer than one interval (report only — still 200 so the
    # container isn't force-restarted for a transient TorBox outage).
    age = time.time() - worker.last_tick()
    worker_ok = age < max(settings.poll_interval * 5, 120)
    return JSONResponse({"status": "ok", "worker_ok": worker_ok, "worker_age_s": int(age)})
