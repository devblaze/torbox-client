"""Request shaping for the TorBox client (no network is touched)."""
from types import SimpleNamespace

import pytest

from app import torbox_client


def _client(monkeypatch, bypass_cache: bool):
    """A client whose _get_json records params instead of calling TorBox."""
    c = torbox_client.TorBoxClient("testkey", "https://example.invalid/v1")
    seen: dict = {}

    async def fake_get_json(path, **params):
        seen["path"] = path
        seen.update(params)
        return {"data": []}

    monkeypatch.setattr(c, "_get_json", fake_get_json)
    # Settings is a frozen dataclass, so swap the module reference instead.
    monkeypatch.setattr(torbox_client, "settings",
                        SimpleNamespace(torbox_bypass_cache=bypass_cache))
    return c, seen


@pytest.mark.parametrize("configured,expected", [(True, "true"), (False, "false")])
async def test_my_list_bypass_cache_follows_setting(monkeypatch, configured, expected):
    c, seen = _client(monkeypatch, configured)
    try:
        assert await c.my_list() == []
    finally:
        await c.close()
    assert seen["path"] == "/api/torrents/mylist"
    assert seen["bypass_cache"] == expected


async def test_my_list_explicit_argument_overrides_setting(monkeypatch):
    c, seen = _client(monkeypatch, True)
    try:
        await c.my_list(bypass_cache=False)
    finally:
        await c.close()
    assert seen["bypass_cache"] == "false"


def test_bypass_cache_defaults_to_on():
    """Existing installs keep the freshest listing unless they opt out."""
    from app.config import Settings
    assert Settings().torbox_bypass_cache is True


# --------------------------------------------------------------------------- #
# "Download already queued" is not a failure
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _post_returning(monkeypatch, c, payload):
    async def fake_post(url, **kw):
        return _Resp(payload)

    monkeypatch.setattr(c._client, "post", fake_post)


async def test_duplicate_add_with_an_id_is_adopted(monkeypatch):
    """TorBox named the existing torrent, so use it rather than failing."""
    c = torbox_client.TorBoxClient("k", "https://example.invalid/v1")
    _post_returning(monkeypatch, c, {
        "success": False, "detail": "Download already queued.",
        "data": {"torrent_id": 4242, "hash": "a" * 40},
    })
    try:
        assert (await c.add_magnet("magnet:?xt=urn:btih:" + "a" * 40))["torrent_id"] == 4242
    finally:
        await c.close()


async def test_duplicate_add_without_an_id_raises_the_duplicate_error(monkeypatch):
    """Distinguishable from a real failure so the caller can look it up."""
    c = torbox_client.TorBoxClient("k", "https://example.invalid/v1")
    _post_returning(monkeypatch, c, {"success": False, "detail": "Download already queued."})
    try:
        with pytest.raises(torbox_client.TorBoxDuplicateError):
            await c.add_magnet("magnet:?xt=urn:btih:" + "a" * 40)
    finally:
        await c.close()


async def test_a_real_failure_is_still_a_plain_error(monkeypatch):
    c = torbox_client.TorBoxClient("k", "https://example.invalid/v1")
    _post_returning(monkeypatch, c, {"success": False, "detail": "Invalid magnet."})
    try:
        with pytest.raises(torbox_client.TorBoxError) as exc:
            await c.add_magnet("magnet:?xt=urn:btih:" + "a" * 40)
        assert not isinstance(exc.value, torbox_client.TorBoxDuplicateError)
    finally:
        await c.close()
