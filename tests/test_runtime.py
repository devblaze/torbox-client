import json

import pytest

from app import runtime
from app.store import Store


@pytest.fixture
def rt_store(tmp_path, monkeypatch):
    """runtime bound to an isolated store; restores real state afterwards."""
    st = Store(str(tmp_path / "state.db"))
    monkeypatch.setattr(runtime, "store", st)
    runtime.load()
    yield st
    monkeypatch.undo()
    runtime.load()


def test_defaults_come_from_env(rt_store):
    assert runtime.get("max_download_speed") == 0
    assert runtime.get("torbox_cleanup_hours") == 24
    assert runtime.get("cloud_max_age_days") == 0
    assert runtime.get("sub_warn_days") == 7
    assert runtime.get("error_burst_threshold") == 25
    assert runtime.get("pushover_token") == ""
    assert runtime.get("pushover_enabled") is True


def test_update_persists_and_survives_reload(rt_store):
    runtime.update({"max_download_speed": 5.5, "pushover_enabled": False,
                    "pushover_token": "  tok  "})
    assert runtime.get("max_download_speed") == 5.5
    assert runtime.get("pushover_token") == "tok"  # stripped
    runtime.load()  # simulate restart
    assert runtime.get("max_download_speed") == 5.5
    assert runtime.get("pushover_enabled") is False


@pytest.mark.parametrize("changes", [
    {"max_download_speed": -1},          # below minimum
    {"max_download_speed": "abc"},       # not a number
    {"max_download_speed": True},        # bool is not a number
    {"sub_warn_days": 2.5},              # int field, fractional
    {"pushover_enabled": "yes"},         # bool field, string
    {"pushover_token": 42},              # str field, number
    {"no_such_setting": 1},              # unknown key
])
def test_update_rejects_bad_values_without_applying(rt_store, changes):
    before = runtime.as_dict()
    with pytest.raises(ValueError):
        runtime.update(changes)
    assert runtime.as_dict() == before


def test_update_is_all_or_nothing(rt_store):
    with pytest.raises(ValueError):
        runtime.update({"max_download_speed": 9, "sub_warn_days": -1})
    assert runtime.get("max_download_speed") == 0  # valid part not applied either


def test_int_field_accepts_whole_float(rt_store):
    runtime.update({"sub_warn_days": 10.0})
    assert runtime.get("sub_warn_days") == 10
    assert isinstance(runtime.get("sub_warn_days"), int)


def test_bad_stored_value_falls_back_to_default(rt_store):
    rt_store.set_kv("setting.max_download_speed", "not-json")
    rt_store.set_kv("setting.sub_warn_days", json.dumps(-5))
    rt_store.set_kv("setting.from_the_future", json.dumps(1))
    runtime.load()
    assert runtime.get("max_download_speed") == 0
    assert runtime.get("sub_warn_days") == 7
