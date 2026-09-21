"""Async client for the TorBox v1 API.

Only the endpoints we need for the Sonarr/Radarr flow are implemented:
add torrent, list torrents, request a per-file download link, and control
(delete) a torrent. Auth is a Bearer token; ``requestdl`` also accepts the key
as a query ``token`` which we use for the CDN redirect.

Docs: https://api-docs.torbox.app/
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .config import settings

log = logging.getLogger("torbox")


class TorBoxError(Exception):
    pass


class TorBoxDuplicateError(TorBoxError):
    """TorBox already holds this torrent — not a failure, just already there."""


# How TorBox words its refusal when the infohash is already in the account.
_DUPLICATE_MARKERS = ("already queued", "already in queue", "already added", "duplicate")


class TorBoxClient:
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    async def _get_json(self, path: str, **params: Any) -> Any:
        resp = await self._client.get(self._url(path), params=params)
        resp.raise_for_status()
        return resp.json()

    # --- account ---
    async def validate_key(self) -> bool:
        try:
            data = await self._get_json("/api/user/me")
            return bool(data.get("success", True))
        except Exception as exc:  # noqa: BLE001
            log.warning("TorBox key validation failed: %s", exc)
            return False

    async def user_me(self) -> dict:
        """Account info: plan, premium_expires_at, etc."""
        data = await self._get_json("/api/user/me")
        return data.get("data") or {}

    # --- torrents ---
    async def add_magnet(self, magnet: str, name: Optional[str] = None) -> dict:
        form = {
            "magnet": magnet,
            "seed": str(settings.torbox_seed),
            "allow_zip": "true" if settings.torbox_allow_zip else "false",
        }
        if name:
            form["name"] = name
        return await self._create_torrent(data=form)

    async def add_torrent_file(self, content: bytes, filename: str = "file.torrent") -> dict:
        form = {
            "seed": str(settings.torbox_seed),
            "allow_zip": "true" if settings.torbox_allow_zip else "false",
        }
        files = {"file": (filename, content, "application/x-bittorrent")}
        return await self._create_torrent(data=form, files=files)

    async def _create_torrent(self, data: dict, files: Optional[dict] = None) -> dict:
        resp = await self._client.post(
            self._url("/api/torrents/createtorrent"), data=data, files=files
        )
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            resp.raise_for_status()
            raise TorBoxError("Non-JSON response from createtorrent")
        if not payload.get("success", False):
            detail = str(payload.get("detail") or f"createtorrent failed ({resp.status_code})")
            if any(m in detail.lower() for m in _DUPLICATE_MARKERS):
                # The torrent is already in the account, which is not a reason
                # to throw away the grab. If TorBox named it, adopt it here;
                # otherwise let the caller find it by infohash.
                existing = payload.get("data")
                if isinstance(existing, dict) and existing.get("torrent_id") is not None:
                    log.info("TorBox already had this torrent (id=%s)", existing.get("torrent_id"))
                    return existing
                raise TorBoxDuplicateError(detail)
            raise TorBoxError(detail)
        # data is usually {"torrent_id": .., "hash": .., "auth_id": ..}
        return payload.get("data") or {}

    async def my_list(self, torrent_id: Optional[int] = None,
                      bypass_cache: Optional[bool] = None) -> list[dict]:
        """List the account's torrents.

        ``bypass_cache`` defaults to the ``TORBOX_BYPASS_CACHE`` setting: an
        uncached listing is the freshest view but by far the most expensive
        request we make, so it is worth being able to turn off.
        """
        if bypass_cache is None:
            bypass_cache = settings.torbox_bypass_cache
        params: dict[str, Any] = {"bypass_cache": "true" if bypass_cache else "false"}
        if torrent_id is not None:
            params["id"] = torrent_id
        data = await self._get_json("/api/torrents/mylist", **params)
        result = data.get("data")
        if result is None:
            return []
        if isinstance(result, dict):  # single-id queries return an object
            return [result]
        return result

    async def request_dl(self, torrent_id: int, file_id: int) -> str:
        """Return a direct CDN URL for one file (valid ~3h)."""
        data = await self._get_json(
            "/api/torrents/requestdl",
            token=self.api_key,
            torrent_id=torrent_id,
            file_id=file_id,
            redirect="false",
        )
        url = data.get("data")
        if not url or not isinstance(url, str):
            raise TorBoxError(f"requestdl returned no url for torrent {torrent_id} file {file_id}")
        return url

    async def control(self, torrent_id: int, operation: str) -> None:
        """operation ∈ {delete, pause, resume, reannounce}."""
        resp = await self._client.post(
            self._url("/api/torrents/controltorrent"),
            json={"torrent_id": torrent_id, "operation": operation},
        )
        # Deleting an already-gone torrent shouldn't be fatal.
        if resp.status_code >= 400:
            log.warning("controltorrent %s on %s -> %s", operation, torrent_id, resp.status_code)

    def stream(self, url: str, headers: Optional[dict] = None):
        """Return a streaming GET context manager for a CDN url.

        The read timeout doubles as stall detection: if no bytes arrive for
        ``STALL_TIMEOUT`` seconds the stream raises instead of hanging forever.
        """
        timeout = httpx.Timeout(connect=15.0, read=settings.stall_timeout, write=60.0, pool=60.0)
        return self._client.stream("GET", url, headers=headers, timeout=timeout)


client = TorBoxClient(settings.torbox_api_key, settings.torbox_base_url)
