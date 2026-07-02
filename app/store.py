"""SQLite-backed state for tracked torrents and categories.

Synchronous sqlite3 access is wrapped in ``asyncio.to_thread`` by callers so it
never blocks the event loop. A single module-level lock serialises writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import settings

# Lifecycle states we track internally (distinct from the qBittorrent states we
# report to the *arr apps, which are derived in qbit_api.py).
STATE_QUEUED = "queued"        # accepted, waiting for TorBox to pick it up
STATE_CLOUD = "cloud"          # TorBox is downloading it in the cloud
STATE_DOWNLOADING = "downloading"  # cloud done, we are pulling files locally
STATE_COMPLETED = "completed"  # files fully present on local disk
STATE_ERROR = "error"

_lock = threading.Lock()


@dataclass
class Torrent:
    hash: str
    name: str
    category: str = ""
    torbox_id: Optional[int] = None
    size: int = 0
    added_on: int = 0
    completion_on: int = 0
    save_path: str = ""
    content_path: str = ""
    state: str = STATE_QUEUED
    cloud_progress: float = 0.0
    local_progress: float = 0.0
    dlspeed: int = 0
    error: str = ""
    files: list = field(default_factory=list)
    last_update: int = 0

    @property
    def progress(self) -> float:
        """Overall 0..1 progress. Never reaches 1.0 until files are local."""
        if self.state == STATE_COMPLETED:
            return 1.0
        if self.state == STATE_DOWNLOADING:
            # Cloud phase is done; blend into the top 10% while we pull files.
            return min(0.90 + 0.099 * self.local_progress, 0.999)
        # Cloud phase: cap at 0.90 so Sonarr never imports prematurely.
        return min(self.cloud_progress * 0.90, 0.90)


def _row_to_torrent(row: sqlite3.Row) -> Torrent:
    return Torrent(
        hash=row["hash"],
        name=row["name"],
        category=row["category"] or "",
        torbox_id=row["torbox_id"],
        size=row["size"] or 0,
        added_on=row["added_on"] or 0,
        completion_on=row["completion_on"] or 0,
        save_path=row["save_path"] or "",
        content_path=row["content_path"] or "",
        state=row["state"] or STATE_QUEUED,
        cloud_progress=row["cloud_progress"] or 0.0,
        local_progress=row["local_progress"] or 0.0,
        dlspeed=row["dlspeed"] or 0,
        error=row["error"] or "",
        files=json.loads(row["files_json"]) if row["files_json"] else [],
        last_update=row["last_update"] or 0,
    )


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with _lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS torrents (
                    hash TEXT PRIMARY KEY,
                    torbox_id INTEGER,
                    name TEXT,
                    category TEXT,
                    size INTEGER,
                    added_on INTEGER,
                    completion_on INTEGER,
                    save_path TEXT,
                    content_path TEXT,
                    state TEXT,
                    cloud_progress REAL,
                    local_progress REAL,
                    dlspeed INTEGER,
                    error TEXT,
                    files_json TEXT,
                    last_update INTEGER
                );
                CREATE TABLE IF NOT EXISTS categories (
                    name TEXT PRIMARY KEY,
                    save_path TEXT
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER,
                    hash TEXT,
                    name TEXT,
                    category TEXT,
                    size INTEGER,
                    event TEXT,
                    detail TEXT
                );
                """
            )
            self._conn.commit()

    # --- torrents ---
    def upsert(self, t: Torrent) -> None:
        t.last_update = int(time.time())
        with _lock:
            self._conn.execute(
                """
                INSERT INTO torrents (hash, torbox_id, name, category, size,
                    added_on, completion_on, save_path, content_path, state,
                    cloud_progress, local_progress, dlspeed, error, files_json,
                    last_update)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(hash) DO UPDATE SET
                    torbox_id=excluded.torbox_id,
                    name=excluded.name,
                    category=excluded.category,
                    size=excluded.size,
                    added_on=excluded.added_on,
                    completion_on=excluded.completion_on,
                    save_path=excluded.save_path,
                    content_path=excluded.content_path,
                    state=excluded.state,
                    cloud_progress=excluded.cloud_progress,
                    local_progress=excluded.local_progress,
                    dlspeed=excluded.dlspeed,
                    error=excluded.error,
                    files_json=excluded.files_json,
                    last_update=excluded.last_update
                """,
                (
                    t.hash, t.torbox_id, t.name, t.category, t.size,
                    t.added_on, t.completion_on, t.save_path, t.content_path,
                    t.state, t.cloud_progress, t.local_progress, t.dlspeed,
                    t.error, json.dumps(t.files), t.last_update,
                ),
            )
            self._conn.commit()

    def get(self, hash_: str) -> Optional[Torrent]:
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM torrents WHERE hash = ?", (hash_.lower(),)
            ).fetchone()
        return _row_to_torrent(row) if row else None

    def all(self) -> list[Torrent]:
        with _lock:
            rows = self._conn.execute("SELECT * FROM torrents").fetchall()
        return [_row_to_torrent(r) for r in rows]

    def delete(self, hash_: str) -> None:
        with _lock:
            self._conn.execute("DELETE FROM torrents WHERE hash = ?", (hash_.lower(),))
            self._conn.commit()

    # --- categories ---
    def set_category(self, name: str, save_path: str) -> None:
        with _lock:
            self._conn.execute(
                "INSERT INTO categories (name, save_path) VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET save_path=excluded.save_path",
                (name, save_path),
            )
            self._conn.commit()

    def remove_category(self, name: str) -> None:
        with _lock:
            self._conn.execute("DELETE FROM categories WHERE name = ?", (name,))
            self._conn.commit()

    def categories(self) -> dict[str, str]:
        with _lock:
            rows = self._conn.execute("SELECT name, save_path FROM categories").fetchall()
        return {r["name"]: r["save_path"] or "" for r in rows}

    # --- history (survives torrent deletion, powers the web UI) ---
    def add_event(self, hash_: str, name: str, category: str, event: str,
                  detail: str = "", size: int = 0) -> None:
        with _lock:
            self._conn.execute(
                "INSERT INTO history (ts, hash, name, category, size, event, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), hash_, name, category, size, event, detail),
            )
            # Keep the table bounded.
            self._conn.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT 1000)"
            )
            self._conn.commit()

    def history(self, limit: int = 200) -> list[dict]:
        with _lock:
            rows = self._conn.execute(
                "SELECT ts, hash, name, category, size, event, detail "
                "FROM history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


store = Store(settings.db_path)
