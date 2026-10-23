"""SQLite store for WhatsApp (Baileys) inbox messages and contacts.

Lets the assistant answer "what did X write to me?" and resolve contact names
to numbers when sending.  Lives in ``<OPENJARVIS_HOME>/whatsapp.db``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.paths import get_config_dir

# Messages older than this are pruned.
_RETENTION_DAYS = 30


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


class WhatsAppStore:
    """Thread-safe store of WhatsApp messages and contacts."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        path = Path(db_path) if db_path else get_config_dir() / "whatsapp.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id  TEXT PRIMARY KEY,
                    chat_jid    TEXT NOT NULL,
                    chat_name   TEXT DEFAULT '',
                    sender_jid  TEXT DEFAULT '',
                    sender_name TEXT DEFAULT '',
                    phone       TEXT DEFAULT '',
                    text        TEXT NOT NULL,
                    ts          REAL NOT NULL,
                    is_group    INTEGER DEFAULT 0,
                    from_me     INTEGER DEFAULT 0,
                    is_read     INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat
                    ON messages(chat_jid, ts);
                CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
                CREATE TABLE IF NOT EXISTS contacts (
                    jid        TEXT PRIMARY KEY,
                    phone      TEXT DEFAULT '',
                    name       TEXT DEFAULT '',
                    push_name  TEXT DEFAULT '',
                    updated_at REAL NOT NULL
                );
                """
            )
            self._conn.commit()

    # -- contacts -----------------------------------------------------------------

    def upsert_contact(
        self, jid: str, *, phone: str = "", name: str = "", push_name: str = ""
    ) -> None:
        if not jid:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO contacts (jid, phone, name, push_name, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(jid) DO UPDATE SET
                    phone = CASE WHEN excluded.phone != ''
                                 THEN excluded.phone ELSE contacts.phone END,
                    name = CASE WHEN excluded.name != ''
                                THEN excluded.name ELSE contacts.name END,
                    push_name = CASE WHEN excluded.push_name != ''
                                     THEN excluded.push_name
                                     ELSE contacts.push_name END,
                    updated_at = excluded.updated_at
                """,
                (jid, _digits(phone), name, push_name, time.time()),
            )
            self._conn.commit()

    def contact_name(self, jid: str = "", phone: str = "") -> str:
        """Address-book name for a JID or phone, or ``""`` if not a contact."""
        row = self._find_contact(jid, phone)
        return row["name"] if row else ""

    def _find_contact(self, jid: str, phone: str) -> Optional[sqlite3.Row]:
        with self._lock:
            if jid:
                row = self._conn.execute(
                    "SELECT * FROM contacts WHERE jid = ? AND name != ''", (jid,)
                ).fetchone()
                if row:
                    return row
            if phone:
                return self._conn.execute(
                    "SELECT * FROM contacts WHERE phone = ? AND name != '' "
                    "ORDER BY updated_at DESC",
                    (_digits(phone),),
                ).fetchone()
        return None

    def search_contacts(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Find contacts whose name, push name or number matches *query*."""
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        num = _digits(q)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT jid, phone, name, push_name FROM contacts
                WHERE name LIKE ? COLLATE NOCASE
                   OR push_name LIKE ? COLLATE NOCASE
                   OR (? != '' AND phone LIKE ?)
                ORDER BY (name != '') DESC, updated_at DESC
                LIMIT ?
                """,
                (like, like, num, f"%{num}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- messages -----------------------------------------------------------------

    def add_message(self, msg: Dict[str, Any]) -> bool:
        """Store an inbox message; returns False if it was already stored."""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    message_id, chat_jid, chat_name, sender_jid, sender_name,
                    phone, text, ts, is_group, from_me, is_read
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.get("message_id") or f"{msg.get('chat_jid')}:{time.time()}",
                    msg.get("chat_jid", ""),
                    msg.get("chat_name", ""),
                    msg.get("sender_jid", ""),
                    msg.get("sender_name", ""),
                    _digits(msg.get("phone", "")),
                    msg.get("text", ""),
                    float(msg.get("ts") or time.time()),
                    1 if msg.get("is_group") else 0,
                    1 if msg.get("from_me") else 0,
                    1 if msg.get("from_me") else 0,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def recent_messages(
        self,
        *,
        query: str = "",
        unread_only: bool = False,
        since_hours: float = 0,
        include_groups: bool = True,
        limit: int = 30,
        mark_read: bool = True,
    ) -> List[Dict[str, Any]]:
        """Messages newest-first, optionally filtered by contact/chat *query*."""
        where = ["1=1"]
        args: List[Any] = []
        if query:
            like = f"%{query.strip()}%"
            num = _digits(query)
            where.append(
                "(chat_name LIKE ? COLLATE NOCASE OR sender_name LIKE ? COLLATE NOCASE"
                " OR (? != '' AND phone LIKE ?))"
            )
            args += [like, like, num, f"%{num}%"]
        if unread_only:
            where.append("is_read = 0")
        if since_hours:
            where.append("ts >= ?")
            args.append(time.time() - since_hours * 3600)
        if not include_groups:
            where.append("is_group = 0")
        sql = (
            "SELECT * FROM messages WHERE "
            + " AND ".join(where)
            + " ORDER BY ts DESC LIMIT ?"
        )
        args.append(limit)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(sql, args).fetchall()]
            if mark_read and rows:
                self._conn.executemany(
                    "UPDATE messages SET is_read = 1 WHERE message_id = ?",
                    [(r["message_id"],) for r in rows],
                )
                self._conn.commit()
        return rows

    def prune(self, days: int = _RETENTION_DAYS) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM messages WHERE ts < ?", (time.time() - days * 86400,)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["WhatsAppStore"]
