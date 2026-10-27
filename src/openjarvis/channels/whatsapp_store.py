"""SQLite store for WhatsApp (Baileys) inbox messages and contacts.

Lets the assistant answer "what did X write to me?" and resolve contact names
to numbers when sending.  Lives in ``<OPENJARVIS_HOME>/whatsapp.db``.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.paths import get_config_dir

# Messages older than this are pruned.
_RETENTION_DAYS = 30


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _fold(text: str) -> str:
    """Lowercase, accent-free text ("Óscar Peña" -> "oscar pena")."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _close(a: str, b: str) -> bool:
    """True if *a* and *b* differ by at most one edit (typos like "osacr")."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            if i + 1 < len(a) and a[i] == b[j + 1] and a[i + 1] == b[j]:
                i += 2  # swapped letters
                j += 2
                continue
            i += 1
        j += 1
    return edits + (len(b) - j) + (len(a) - i) <= 1


def _token_score(q: str, words: List[str]) -> float:
    """How well one query word matches a name: 1 exact, .9 prefix, .7 typo."""
    best = 0.0
    for w in words:
        if w == q:
            return 1.0
        if w.startswith(q) or (len(w) >= 3 and q.startswith(w)):
            best = max(best, 0.9)
        elif len(q) >= 4 and _close(q, w):
            best = max(best, 0.7)
    return best


def _name_score(query: str, name: str) -> float:
    """Score 0..1 of *name* for *query*; every query word must match."""
    q_words = _fold(query).split()
    words = _fold(name).split()
    if not q_words or not words:
        return 0.0
    scores = [_token_score(q, words) for q in q_words]
    if min(scores) == 0:
        return 0.0
    score = sum(scores) / len(scores)
    # Prefer names without many extra words ("Oscar Peña" over "Oscar Peña
    # Fontanero Casa") and matches in the same order.
    score -= 0.02 * max(0, len(words) - len(q_words))
    if " ".join(words).startswith(" ".join(q_words)):
        score += 0.05
    return score


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
        """Find contacts by name, WhatsApp name or number, best match first.

        Accent- and case-insensitive, word order free, tolerant to one typo
        per word and to partial words ("osc peña" finds "Óscar Peña López").
        People who wrote to the user but are not saved as contacts are found
        through the names seen in stored messages.
        """
        q = (query or "").strip()
        if not q:
            return []
        num = _digits(q)
        is_number = len(num) >= 6 and len(num) >= len(q.replace(" ", "")) - 3
        with self._lock:
            contacts = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT jid, phone, name, push_name, updated_at FROM contacts"
                ).fetchall()
            ]
            seen = self._conn.execute(
                """
                SELECT sender_jid AS jid, phone, sender_name AS push_name,
                       MAX(ts) AS updated_at
                FROM messages
                WHERE is_group = 0 AND from_me = 0 AND sender_name != ''
                GROUP BY sender_jid
                """
            ).fetchall()
        known = {c["jid"] for c in contacts} | {
            c["phone"] for c in contacts if c["phone"]
        }
        for r in seen:
            if r["jid"] not in known and (not r["phone"] or r["phone"] not in known):
                contacts.append({**dict(r), "name": ""})

        scored = []
        for c in contacts:
            if is_number:
                score = 1.0 if c["phone"] and num in c["phone"] else 0.0
            else:
                score = max(
                    _name_score(q, c["name"] or ""),
                    _name_score(q, c["push_name"] or "") - 0.05,
                )
            if score > 0:
                scored.append((score, bool(c["name"]), c["updated_at"] or 0, c))
        scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
        out = []
        for score, _, _, c in scored[:limit]:
            out.append(
                {
                    "jid": c["jid"],
                    "phone": c["phone"] or "",
                    "name": c["name"] or "",
                    "push_name": c["push_name"] or "",
                    "score": round(score, 3),
                }
            )
        return out

    def contact_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE name != ''"
            ).fetchone()[0]

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
