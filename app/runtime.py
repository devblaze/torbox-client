"""Runtime-tunable settings, editable from the web UI's Settings tab.

Environment variables (config.settings) provide the defaults; anything saved
from the UI is persisted in the SQLite ``kv`` table and overrides the env value
on every start. Hot paths (the download rate limiter) call ``get()`` on every
use, so a change saved in the UI applies immediately — no restart needed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .config import settings
from .store import store

log = logging.getLogger("runtime")

_PREFIX = "setting."


@dataclass(frozen=True)
class _Field:
    kind: str  # "float" | "int" | "bool" | "str"
    default: Any
    minimum: float | None = None


FIELDS: dict[str, _Field] = {
    # Aggregate local download cap in MiB/s across all files (0 = unlimited).
    "max_download_speed": _Field("float", settings.max_download_speed, 0),
    # Hours after local completion before the cloud copy is deleted (0 = never).
    "torbox_cleanup_hours": _Field("float", settings.torbox_cleanup_hours, 0),
    # Delete any TorBox item older than this many days (0 = off).
    "cloud_max_age_days": _Field("float", settings.cloud_max_age_days, 0),
    # Warn when the subscription has this many days left (0 = off).
    "sub_warn_days": _Field("int", settings.sub_warn_days, 0),
    # Pushover alert after this many errors in a 15-minute window (0 = off).
    "error_burst_threshold": _Field("int", settings.error_burst_threshold, 0),
    "pushover_enabled": _Field("bool", settings.pushover_enabled),
    "pushover_token": _Field("str", settings.pushover_token),
    "pushover_user": _Field("str", settings.pushover_user),
    # How many History-tab events to keep before pruning the oldest.
    "history_retention": _Field("int", settings.history_retention, 100),
}

_values: dict[str, Any] = {}


def _coerce(name: str, field: _Field, value: Any) -> Any:
    if field.kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{name}: expected true/false")
    if field.kind == "str":
        if isinstance(value, str):
            return value.strip()
        raise ValueError(f"{name}: expected a string")
    # bool is an int subclass — reject it explicitly for numeric fields.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name}: expected a number")
    try:
        num: float = float(value)
    except ValueError:
        raise ValueError(f"{name}: expected a number") from None
    if field.kind == "int":
        if num != int(num):
            raise ValueError(f"{name}: expected a whole number")
        num = int(num)
    if field.minimum is not None and num < field.minimum:
        raise ValueError(f"{name}: must be at least {field.minimum:g}")
    return num


def load() -> None:
    """Populate from env defaults, then apply persisted overrides."""
    _values.clear()
    for name, field in FIELDS.items():
        _values[name] = field.default
    for key, raw in store.kv_prefix(_PREFIX).items():
        name = key[len(_PREFIX):]
        field = FIELDS.get(name)
        if field is None:
            continue  # setting from another version of the app — ignore
        try:
            _values[name] = _coerce(name, field, json.loads(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("Ignoring bad stored setting %s=%r (%s)", name, raw, exc)


def get(name: str) -> Any:
    return _values[name]


def as_dict() -> dict[str, Any]:
    return dict(_values)


def update(changes: dict[str, Any]) -> None:
    """Validate and persist a batch of changes; rejects the whole batch on any
    bad field so the UI never half-applies a form."""
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(unknown)}")
    coerced = {name: _coerce(name, FIELDS[name], value) for name, value in changes.items()}
    for name, value in coerced.items():
        _values[name] = value
        store.set_kv(_PREFIX + name, json.dumps(value))
        if name in ("pushover_token", "pushover_user"):
            log.info("Setting %s updated", name)
        else:
            log.info("Setting %s = %r", name, value)


load()
