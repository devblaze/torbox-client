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
