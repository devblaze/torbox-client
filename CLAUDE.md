# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A qBittorrent Web API emulator that lets Sonarr/Radarr use TorBox (a cloud debrid service) as a download client. The *arr apps push releases here exactly as they would to qBittorrent; a background worker hands each one to TorBox, polls until the cloud download finishes, pulls the files to the local downloads dir, and only then reports the torrent completed — progress to *arr is deliberately capped below 100% until every file is on local disk, so imports never fire early.

## Commands

```bash
pip install -r requirements-dev.txt        # includes requirements.txt + pytest
pytest                                     # full suite
pytest tests/test_worker.py                # one file
pytest tests/test_worker.py::test_name     # one test

# Run locally (no Docker):
TORBOX_API_KEY=... DOWNLOAD_DIR=./dl DATA_DIR=./data \
  uvicorn app.main:app --reload --port 8080
```

No linter/formatter is configured. CI (`.github/workflows/docker-publish.yml`) runs `pytest -q` on every push/PR; the multi-arch Docker image publishes to GHCR only after tests pass, and never on PRs.

## Architecture

**Import-time singletons.** `config.settings`, `store.store` (SQLite), and `torbox_client.client` are module-level instances created at import, and `settings` reads the environment at import time. This is why `tests/conftest.py` sets `DATA_DIR`/`DOWNLOAD_DIR`/`TORBOX_API_KEY` env vars *before* any `from app import ...` — any new test setup that touches config must do the same.

**Wiring** (`app/main.py`): the FastAPI lifespan starts two long-lived tasks — `worker.run()` (the poll/download loop) and `worker.housekeeping()` (subscription status, age-based cloud cleanup) — and configures logging so the root logger is DEBUG with three handlers: console (honours `LOG_LEVEL`), the in-memory ring buffer behind the web UI Logs tab (`logbuffer.py`), and the error counter feeding Pushover burst alerts (`notify.py`). Every handler gets a `RedactSecrets` filter so the TorBox API key can never leak to console, the HTTP-exposed log buffer, or an outgoing notification — preserve that when adding handlers.

**Two API surfaces, one auth.** `qbit_api.py` implements the qBittorrent Web API v2 subset Sonarr/Radarr actually use (login, `torrents/info`, `torrents/add`, `torrents/files`, `torrents/delete`) and translates it to TorBox + local state. `webui.py` serves the human dashboard (`static/index.html`, vanilla JS, no build step) exposing the same state in two-phase form (cloud progress vs. local pull). Both share the same in-memory SID-cookie sessions via the `require_auth` dependency; routers are split into public (login, dashboard page) and guarded variants in `main.py`.

**Settings layering** (`config.py` vs `runtime.py`): env vars provide defaults; values saved from the web UI Settings tab persist in the SQLite `kv` table and override env from then on. Hot paths (e.g. the download rate limiter) call `runtime.get()` on every use so changes apply immediately without restart. When adding a tunable, decide which layer it belongs in.

**Worker state** (`worker.py`): keeps module-level mutable state (`_downloading`, `_attempts`, `_background_tasks`, `_subscription`). Tests use the `worker_env` fixture in `conftest.py`, which swaps in a fresh `Store` and clears all of it — extend that fixture if you add module-level state.

**Store** (`store.py`): synchronous `sqlite3` under a module lock, called directly from the event loop *by design* — the working set is tens of rows and each call is sub-millisecond. Don't convert it to async unless the tracked set grows large.

**Security guards** in `qbit_api.py`: fetching a `.torrent` by URL goes through an SSRF check (resolves the host and blocks loopback/private/link-local/reserved ranges) plus a size cap. The SSRF tests stay hermetic by mocking `socket.getaddrinfo` — follow that pattern rather than doing real DNS in tests.

**Infohash** (`bencode.py`): *arr apps track queue items by the real v1 infohash, so it's computed locally (parsed from magnet `btih`, or by hashing the bencoded `info` dict of an uploaded `.torrent`) before anything is sent to TorBox.

## Testing notes

- `pyproject.toml` sets `asyncio_mode = "auto"` (async tests need no marker) and `pythonpath = ["."]` (bare `pytest` works from the repo root).
- Fixtures: `store` (fresh SQLite-backed `Store`), `worker_env` (fresh store wired into `worker` + module state cleared).

## Hosting note

This repo lives on **GitHub** (`devblaze/torbox-client`, images on GHCR) — an exception to the user's global "all remotes are Gitea / use `tea`" instruction. `gh` is not installed; use `git` and `curl` against the GitHub API if needed.
