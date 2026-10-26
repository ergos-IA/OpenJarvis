"""SQLite store for reminders and remembered facts (``<home>/personal.db``)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.paths import get_config_dir

# Facts are injected into every prompt: keep the list short.
MAX_FACTS = 60


class PersonalStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        path = Path(db_path) if db_path else get_config_dir() / "personal.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    is_task INTEGER NOT NULL DEFAULT 0,
                    next_run REAL NOT NULL,
                    cron TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created REAL NOT NULL,
                    last_run REAL
                );
                CREATE INDEX IF NOT EXISTS reminders_due
                    ON reminders (status, next_run);
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    created REAL NOT NULL
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- reminders --------------------------------------------------------

    def add_reminder(
        self, text: str, next_run: float, *, cron: str = "", is_task: bool = False
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders (text, is_task, next_run, cron, created) "
                "VALUES (?, ?, ?, ?, ?)",
                (text, int(is_task), next_run, cron, time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_reminder(self, reminder_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return dict(row) if row else None

    def active_reminders(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE status = 'active' ORDER BY next_run"
            ).fetchall()
        return [dict(r) for r in rows]

    def due_reminders(self, now: float) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE status = 'active' AND next_run <= ? "
                "ORDER BY next_run",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_reminder(self, reminder_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE reminders SET {cols} WHERE id = ?",
                (*fields.values(), reminder_id),
            )
            self._conn.commit()

    # -- facts ------------------------------------------------------------

    def add_fact(self, text: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts (text, created) VALUES (?, ?)", (text, time.time())
            )
            # Oldest facts fall off once the list is full.
            self._conn.execute(
                "DELETE FROM facts WHERE id NOT IN "
                "(SELECT id FROM facts ORDER BY id DESC LIMIT ?)",
                (MAX_FACTS,),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def facts(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM facts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def delete_fact(self, fact_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            self._conn.commit()
            return cur.rowcount > 0


__all__ = ["MAX_FACTS", "PersonalStore"]
