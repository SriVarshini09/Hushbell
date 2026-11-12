"""Local storage: items, the messages behind them, and an audit log of every action.

Everything stays in one SQLite file in the data folder. Nothing is sent anywhere
except the message text Hushbell asks Claude to sort.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from hushbell.models import Item

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    app TEXT NOT NULL,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    child TEXT NOT NULL DEFAULT '',
    due TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL DEFAULT 'none',
    reply_text TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    rule_hit TEXT NOT NULL DEFAULT '',
    sorted_by TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]',
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    call_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at REAL NOT NULL,
    item_id TEXT NOT NULL,
    app TEXT NOT NULL,
    package TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'phone'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    item_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    fp TEXT PRIMARY KEY,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
"""

_ITEM_FIELDS = [
    "id", "created_at", "updated_at", "app", "sender", "text", "status", "category",
    "reason", "summary", "child", "due", "action_type", "reply_text", "url",
    "instructions", "rule_hit", "sorted_by", "result", "sources", "duplicate_count",
    "call_attempts",
]


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- items -------------------------------------------------------------

    def create_item(self, app: str, sender: str, text: str, status: str = "new") -> Item:
        now = time.time()
        item = Item(id=uuid.uuid4().hex[:10], created_at=now, updated_at=now, app=app,
                    sender=sender, text=text, status=status, sources=[app])
        with self._lock:
            self._conn.execute(
                f"INSERT INTO items ({', '.join(_ITEM_FIELDS)}) VALUES ({', '.join('?' * len(_ITEM_FIELDS))})",
                [self._to_row(item)[f] for f in _ITEM_FIELDS],
            )
            self._conn.commit()
        return item

    def get_item(self, item_id: str) -> Item | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._from_row(row) if row else None

    def update_item(self, item_id: str, **fields) -> Item | None:
        if not fields:
            return self.get_item(item_id)
        if "sources" in fields:
            fields["sources"] = json.dumps(fields["sources"])
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE items SET {cols} WHERE id = ?", [*fields.values(), item_id])
            self._conn.commit()
        return self.get_item(item_id)

    def transition(self, item_id: str, allowed_from: set[str], to: str) -> bool:
        """Atomically move an item to `to` only if it is currently in `allowed_from`."""
        with self._lock:
            row = self._conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row or row["status"] not in allowed_from:
                return False
            self._conn.execute("UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
                               (to, time.time(), item_id))
            self._conn.commit()
            return True

    def list_items(self, since: float = 0.0, limit: int = 200) -> list[Item]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [self._from_row(r) for r in rows]

    def recent_items(self, window_seconds: float) -> list[Item]:
        return self.list_items(since=time.time() - window_seconds, limit=500)

    # ---- messages ----------------------------------------------------------

    def add_message(self, item_id: str, app: str, package: str, sender: str, text: str, source: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (received_at, item_id, app, package, sender, text, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), item_id, app, package, sender, text, source),
            )
            self._conn.commit()

    def count_messages(self, since: float = 0.0) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM messages WHERE received_at >= ?",
                                     (since,)).fetchone()
        return int(row["n"])

    # ---- audit log ---------------------------------------------------------

    def add_event(self, kind: str, detail: str = "", item_id: str = "") -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events (at, item_id, kind, detail) VALUES (?, ?, ?, ?)",
                               (time.time(), item_id, kind, detail))
            self._conn.commit()

    def list_events(self, limit: int = 100, since: float = 0.0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE at >= ? ORDER BY id DESC LIMIT ?", (since, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- phone notifications already seen ----------------------------------

    def load_seen(self, keep: int = 5000) -> set[str]:
        """Fingerprints of notifications Hushbell has already looked at (kept across restarts)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM seen WHERE fp NOT IN (SELECT fp FROM seen ORDER BY at DESC LIMIT ?)", (keep,))
            rows = self._conn.execute("SELECT fp FROM seen").fetchall()
            self._conn.commit()
        return {r["fp"] for r in rows}

    def mark_seen(self, fingerprints: list[str]) -> None:
        if not fingerprints:
            return
        now = time.time()
        with self._lock:
            self._conn.executemany("INSERT OR IGNORE INTO seen (fp, at) VALUES (?, ?)",
                                   [(fp, now) for fp in fingerprints])
            self._conn.commit()

    # ---- small key/value settings -----------------------------------------

    def get_kv(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            self._conn.commit()

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _to_row(item: Item) -> dict:
        row = {f: getattr(item, f) for f in _ITEM_FIELDS}
        row["sources"] = json.dumps(item.sources)
        return row

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Item:
        data = {f: row[f] for f in _ITEM_FIELDS}
        data["sources"] = json.loads(data["sources"] or "[]")
        return Item(**data)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
