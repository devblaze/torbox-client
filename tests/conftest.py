"""Shared test fixtures.

Environment is set BEFORE any ``app`` import because config.Settings and the
store singleton read the environment at import time. Everything is pointed at a
throwaway temp dir so tests never touch a real /data or /downloads.
"""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="torbox-tests-")
os.environ.setdefault("DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("DOWNLOAD_DIR", os.path.join(_TMP, "downloads"))
os.environ.setdefault("SAVE_PATH", os.path.join(_TMP, "downloads"))
os.environ.setdefault("TORBOX_API_KEY", "testkey-abcdef123456")
os.makedirs(os.environ["DOWNLOAD_DIR"], exist_ok=True)

import pytest  # noqa: E402

from app.store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """A fresh Store backed by an isolated sqlite file."""
    return Store(str(tmp_path / "state.db"))


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """Point the worker at a fresh store and clear its module-level state."""
    from app import worker

    st = Store(str(tmp_path / "state.db"))
    monkeypatch.setattr(worker, "store", st)
    worker._downloading.clear()
    worker._attempts.clear()
    worker._background_tasks.clear()
    worker._subscription.clear()
    return st
