"""SQLite-backed state for tracked torrents and categories.

Access is synchronous sqlite3 guarded by a module-level lock. Calls happen from
the asyncio event loop, so each operation briefly blocks it; that is acceptable
here because the working set is small (tens of rows) and every statement is
indexed by primary key, keeping each call sub-millisecond. If the tracked set
ever grows large, move these calls behind ``asyncio.to_thread``.
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
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT
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
                -- The web UI filters history by age and by event/category, and
                -- sorts by name; these keep those queries off a full scan once
                -- retention is raised past a few thousand rows.
                CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts);
                CREATE INDEX IF NOT EXISTS idx_history_event ON history(event);
                CREATE INDEX IF NOT EXISTS idx_history_category ON history(category);
                """
            )
            self._conn.commit()

    # --- torrents ---
    def upsert(self, t: Torrent) -> None:
        t.last_update = int(time.time())
        # get()/delete() look up by lowercased hash, so store it lowercased too;
        # otherwise an upper/mixed-case hash would be written but never found.
        t.hash = t.hash.lower()
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

    # --- generic key/value (runtime settings overrides, notification state) ---
    def set_kv(self, key: str, value: str) -> None:
        with _lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_kv(self, key: str) -> Optional[str]:
        with _lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_prefix(self, prefix: str) -> dict[str, str]:
        with _lock:
            rows = self._conn.execute(
                "SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",)
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- history (survives torrent deletion, powers the web UI) ---
    # Fallback retention, used before runtime settings are loaded. The live value
    # is the ``history_retention`` runtime setting, editable in the UI.
    _HISTORY_LIMIT = 1000

    # Whitelist of sortable columns: the sort key arrives from the UI as a query
    # string, so it must never be interpolated into SQL unchecked.
    _HISTORY_SORTS = {"ts": "id", "name": "name", "size": "size", "event": "event"}

    def _retention(self) -> int:
        # Deferred import: runtime imports this module at load time, so it can
        # only be reached once both modules are initialised.
        try:
            from . import runtime
            return max(int(runtime.get("history_retention")), 1)
        except (ImportError, KeyError, TypeError, ValueError):
            return self._HISTORY_LIMIT

    def add_event(self, hash_: str, name: str, category: str, event: str,
                  detail: str = "", size: int = 0) -> None:
        keep = self._retention()
        with _lock:
            cur = self._conn.execute(
                "INSERT INTO history (ts, hash, name, category, size, event, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), hash_, name, category, size, event, detail),
            )
            # Prune occasionally rather than on every insert: the trim is a full
            # anti-join scan, so amortise it instead of paying it each event.
            if cur.lastrowid and cur.lastrowid % 100 == 0:
                self._conn.execute(
                    "DELETE FROM history WHERE id NOT IN "
                    "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                    (keep,),
                )
            self._conn.commit()

    def prune_history(self) -> int:
        """Trim history to the current retention now, returning rows removed.

        ``add_event`` only prunes every hundredth insert, so lowering the
        retention setting needs an explicit trim for the change to be visible
        straight away instead of up to 99 events later.
        """
        keep = self._retention()
        with _lock:
            cur = self._conn.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT ?)", (keep,)
            )
            self._conn.commit()
        return cur.rowcount

    def history(self, limit: int = 200) -> list[dict]:
        """Newest-first history, unfiltered. See ``history_page`` for the UI."""
        return self.history_page(limit=limit)[0]

    def history_page(self, *, limit: int = 50, offset: int = 0, search: str = "",
                     events: Optional[list[str]] = None,
                     categories: Optional[list[str]] = None,
                     since: int = 0, sort: str = "ts",
                     order: str = "desc") -> tuple[list[dict], int]:
        """One page of history plus the total number of matching rows.

        ``categories`` matches on the stored value, so an empty string in the
        list selects uncategorised events. Unknown ``sort`` keys fall back to
        chronological order rather than erroring — the UI is the only caller.
        """
        where: list[str] = []
        params: list = []
        if search:
            like = f"%{search}%"
            where.append("(name LIKE ? OR detail LIKE ? OR hash LIKE ?)")
            params += [like, like, like]
        if events:
            where.append(f"event IN ({','.join('?' * len(events))})")
            params += list(events)
        if categories:
            where.append(
                f"COALESCE(category, '') IN ({','.join('?' * len(categories))})")
            params += list(categories)
        if since:
            where.append("ts >= ?")
            params.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        col = self._HISTORY_SORTS.get(sort, "id")
        direction = "ASC" if order == "asc" else "DESC"
        # id is the tiebreaker so rows with equal keys keep a stable order across
        # pages; on the chronological sort it *is* the key (ids are monotonic).
        if col == "id":
            order_by = f"id {direction}"
        elif col == "name":
            order_by = f"name COLLATE NOCASE {direction}, id DESC"
        else:
            order_by = f"{col} {direction}, id DESC"

        with _lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM history" + clause, params).fetchone()["n"]
            rows = self._conn.execute(
                "SELECT ts, hash, name, category, size, event, detail FROM history"
                + clause + f" ORDER BY {order_by} LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    def history_count(self) -> int:
        """Total retained events, ignoring filters — the UI shows it alongside
        the filtered count so a search says how much it narrowed things down."""
        with _lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS n FROM history").fetchone()["n"]

    def history_facets(self) -> dict[str, list[str]]:
        """Distinct event kinds and categories, for the UI's filter dropdowns."""
        with _lock:
            events = self._conn.execute(
                "SELECT DISTINCT event FROM history ORDER BY event").fetchall()
            categories = self._conn.execute(
                "SELECT DISTINCT COALESCE(category, '') AS c FROM history "
                "ORDER BY c").fetchall()
        return {
            "events": [r["event"] for r in events if r["event"]],
            "categories": [r["c"] for r in categories],
        }


store = Store(settings.db_path)
