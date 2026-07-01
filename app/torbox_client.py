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
            raise TorBoxError(payload.get("detail") or f"createtorrent failed ({resp.status_code})")
        # data is usually {"torrent_id": .., "hash": .., "auth_id": ..}
        return payload.get("data") or {}

    async def my_list(self, torrent_id: Optional[int] = None, bypass_cache: bool = True) -> list[dict]:
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
        """Return a streaming GET context manager for a CDN url."""
        return self._client.stream("GET", url, headers=headers, timeout=None)


client = TorBoxClient(settings.torbox_api_key, settings.torbox_base_url)
