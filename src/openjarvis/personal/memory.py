# ruff: noqa: E501
"""Long-term memory of the personal assistant (``<home>/memory.db``).

Three layers, all permanent:

* **log** -- every message of the user's chat with the assistant, searchable
  (SQLite FTS5).  Nothing is ever dropped.
* **facts** -- what the assistant knows about the user (people, preferences,
  work, projects, plans...).  The user can add them ("recuerda que...") and the
  assistant learns them on its own after each exchange; facts are updated or
  removed when they change ("ya no trabajo en X"), never silently capped.
* **journal** -- one short summary per day, written in the background, so the
  assistant can recall what happened weeks or months ago.

:meth:`LongMemory.context` builds the block injected in the system prompt on
every message: all facts, the last days of the journal and past snippets
relevant to the current message.  :class:`MemoryKeeper` runs the AI work
(learning facts, writing the journal) on a background thread.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

KINDS = (
    "persona",
    "preferencia",
    "trabajo",
    "proyecto",
    "plan",
    "salud",
    "otro",
)
MAX_FACT_CHARS = 300
# Facts injected in every prompt (most recently updated first).
PROMPT_FACTS = 250
JOURNAL_DAYS_IN_PROMPT = 7

_STOP = {
    "que",
    "de",
    "la",
    "el",
    "en",
    "y",
    "a",
    "los",
    "las",
    "un",
    "una",
    "por",
    "para",
    "con",
    "no",
    "me",
    "mi",
    "se",
    "lo",
    "le",
    "es",
    "al",
    "del",
    "te",
    "tu",
    "su",
    "si",
    "ya",
    "como",
    "pero",
    "mas",
    "hay",
    "eso",
    "esto",
    "esa",
    "ese",
    "muy",
    "hoy",
    "dime",
    "dile",
    "sobre",
    "cuando",
    "donde",
    "quien",
    "qué",
    "cual",
    "tengo",
    "tiene",
    "hacer",
    "puedes",
    "quiero",
    "jarvis",
}

EXTRACT_PROMPT = """Eres la memoria a largo plazo de un asistente personal. Lees un intercambio entre el usuario y su asistente y decides qué merece recordarse para siempre sobre el usuario.

Recuerda solo datos duraderos y útiles en el futuro: personas de su vida y su relación con ellas (familia, socios, clientes, amigos, con nombres), su trabajo y empresas, proyectos en marcha, preferencias y forma de trabajar, planes y fechas importantes, lugares habituales, decisiones tomadas. NO guardes: peticiones puntuales ya resueltas, saludos, el tiempo que hace, ni lo que diga el asistente de sí mismo. Lo que venga de mensajes o correos de terceros que el asistente cite son datos de otros: guárdalo solo si el usuario lo confirma como suyo.

Te paso los recuerdos que ya existen con su número. Si algo cambia o se precisa, ACTUALIZA el recuerdo existente en vez de duplicarlo; si deja de ser cierto, bórralo.

Responde SOLO con un JSON: una lista de operaciones, o [] si no hay nada.
[{"op":"add","kind":"persona|preferencia|trabajo|proyecto|plan|salud|otro","text":"..."},
 {"op":"update","id":12,"text":"..."},
 {"op":"delete","id":7}]
Cada texto: una frase en tercera persona, concreta, con nombres propios («Óscar Peña es su socio en AI Fast Track»), de menos de 250 caracteres."""

JOURNAL_PROMPT = """Resume en español lo que pasó este día en la conversación entre el usuario y su asistente personal, para que el asistente lo recuerde en el futuro. Entre 2 y 8 viñetas cortas («- …»): qué pidió o decidió el usuario, con quién (nombres), qué hizo el asistente por él, y lo que quedó pendiente. Sin relleno ni saludos. Si no hubo nada relevante, responde solo «-»."""


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _keywords(text: str, limit: int = 8) -> List[str]:
    words = [w for w in _fold(text).split() if len(w) > 2 and w not in _STOP]
    seen: List[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return seen[:limit]


def _parse_json_list(content: str) -> List[Any]:
    content = (content or "").strip()
    match = re.search(r"\[.*\]", content, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    return data if isinstance(data, list) else []


class LongMemory:
    """SQLite store for the log, facts and journal.  Thread-safe."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    channel TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_log_ts ON log(ts);
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    kind TEXT DEFAULT 'otro',
                    source TEXT DEFAULT 'usuario',
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal (
                    day TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )
            self._fts = True
            try:
                self._conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS log_fts USING fts5(
                        text, content='log', content_rowid='id',
                        tokenize='unicode61 remove_diacritics 2'
                    );
                    CREATE TRIGGER IF NOT EXISTS log_ai AFTER INSERT ON log BEGIN
                        INSERT INTO log_fts(rowid, text) VALUES (new.id, new.text);
                    END;
                    """
                )
            except sqlite3.OperationalError:
                self._fts = False
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meta -----------------------------------------------------------------

    def get_meta(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else ""

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # -- log ------------------------------------------------------------------

    def record(
        self, role: str, text: str, *, channel: str = "", ts: Optional[float] = None
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO log (ts, role, text, channel) VALUES (?, ?, ?, ?)",
                (ts or time.time(), role, text, channel),
            )
            self._conn.commit()

    def log_between(self, start: float, end: float) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, role, text FROM log WHERE ts >= ? AND ts < ? ORDER BY ts",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_log(
        self, query: str, *, limit: int = 8, before: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Past messages matching *query* (any keyword), best first."""
        words = _keywords(query)
        if not words:
            return []
        before = before or time.time() + 1
        with self._lock:
            if self._fts:
                expr = " OR ".join(f'"{w}"*' for w in words)
                try:
                    rows = self._conn.execute(
                        """
                        SELECT log.ts, log.role, log.text FROM log_fts
                        JOIN log ON log.id = log_fts.rowid
                        WHERE log_fts MATCH ? AND log.ts < ?
                        ORDER BY bm25(log_fts) LIMIT ?
                        """,
                        (expr, before, limit),
                    ).fetchall()
                    return [dict(r) for r in rows]
                except sqlite3.OperationalError:
                    pass
            like = " OR ".join("text LIKE ?" for _ in words)
            rows = self._conn.execute(
                f"SELECT ts, role, text FROM log WHERE ({like}) AND ts < ? "
                "ORDER BY ts DESC LIMIT ?",
                (*[f"%{w}%" for w in words], before, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def log_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM log").fetchone()[0]

    # -- facts ----------------------------------------------------------------

    def facts(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM facts ORDER BY updated DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_fact(
        self, text: str, *, kind: str = "otro", source: str = "usuario"
    ) -> int:
        text = " ".join((text or "").split())[:MAX_FACT_CHARS]
        if not text:
            raise ValueError("Falta el dato")
        kind = kind if kind in KINDS else "otro"
        key = _fold(text)
        now = time.time()
        with self._lock:
            for row in self._conn.execute("SELECT id, text FROM facts").fetchall():
                if _fold(row["text"]) == key:
                    self._conn.execute(
                        "UPDATE facts SET updated = ? WHERE id = ?", (now, row["id"])
                    )
                    self._conn.commit()
                    return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO facts (text, kind, source, created, updated) "
                "VALUES (?, ?, ?, ?, ?)",
                (text, kind, source, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_fact(self, fact_id: int, text: str) -> bool:
        text = " ".join((text or "").split())[:MAX_FACT_CHARS]
        if not text:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET text = ?, updated = ? WHERE id = ?",
                (text, time.time(), int(fact_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_fact(self, fact_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE id = ?", (int(fact_id),))
            self._conn.commit()
            return cur.rowcount > 0

    def relevant_facts(self, text: str, limit: int = 40) -> List[Dict[str, Any]]:
        """Facts sharing words with *text*, plus the most recent ones."""
        words = set(_keywords(text, 12))
        facts = self.facts()
        scored = sorted(
            facts,
            key=lambda f: (len(words & set(_fold(f["text"]).split())), f["updated"]),
            reverse=True,
        )
        return scored[:limit]

    # -- journal --------------------------------------------------------------

    def journal(self, days: int = 0) -> List[Dict[str, Any]]:
        sql = "SELECT day, summary FROM journal ORDER BY day DESC"
        args: tuple = ()
        if days:
            sql += " LIMIT ?"
            args = (days,)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def set_journal(self, day: str, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO journal (day, summary, created) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET summary = excluded.summary",
                (day, summary.strip(), time.time()),
            )
            self._conn.commit()

    def search_journal(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        words = _keywords(query)
        if not words:
            return []
        out = []
        for j in self.journal():
            folded = _fold(j["summary"])
            score = sum(1 for w in words if w in folded)
            if score:
                out.append((score, j))
        out.sort(key=lambda t: (t[0], t[1]["day"]), reverse=True)
        return [j for _, j in out[:limit]]

    def days_to_journal(self, today: date, max_days: int = 30) -> List[str]:
        """Past days with messages and no journal entry yet (oldest first)."""
        start = time.time() - max_days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') AS d FROM log "
                "WHERE ts >= ? ORDER BY d",
                (start,),
            ).fetchall()
            done = {r["day"] for r in self._conn.execute("SELECT day FROM journal")}
        return [
            r["d"]
            for r in rows
            if r["d"] and r["d"] < today.isoformat() and r["d"] not in done
        ]

    # -- prompt ---------------------------------------------------------------

    def context(self, current: str = "") -> str:
        """Memory block for the system prompt."""
        parts: List[str] = []
        facts = self.facts()
        if facts:
            shown = facts[:PROMPT_FACTS]
            if len(facts) > PROMPT_FACTS and current:
                ids = {f["id"] for f in shown}
                extra = [
                    f for f in self.relevant_facts(current, 30) if f["id"] not in ids
                ]
                shown = extra + shown[: PROMPT_FACTS - len(extra)]
            lines = [f"[{f['id']}] {f['text']}" for f in shown]
            parts.append(
                "Lo que sabes del usuario (tu memoria; úsala siempre, corrígela con "
                "memory_correct si algo cambia):\n" + "\n".join(lines)
            )
        recent = self.journal(JOURNAL_DAYS_IN_PROMPT)
        if recent:
            parts.append(
                "Tu diario de los últimos días:\n"
                + "\n".join(f"{j['day']}:\n{j['summary']}" for j in reversed(recent))
            )
        if current:
            day_ago = time.time() - 86400
            hits = self.search_log(current, limit=6, before=day_ago)
            if hits:
                parts.append(
                    "Conversaciones antiguas relacionadas con este mensaje:\n"
                    + "\n".join(
                        f"({_day(h['ts'])}) {'Usuario' if h['role'] == 'user' else 'Tú'}: "
                        f"{h['text'][:300]}"
                        for h in hits
                    )
                )
        if not parts:
            return ""
        return (
            "\n\nTienes memoria permanente: recuerdas todo lo que has hablado con el "
            "usuario. Si necesitas algo de antes que no ves aquí, búscalo con "
            "memory_recall.\n\n" + "\n\n".join(parts)
        )

    # -- learning -------------------------------------------------------------

    def apply_ops(self, ops: List[Any]) -> int:
        """Apply extraction operations; returns how many changed memory."""
        changed = 0
        for op in ops[:12]:
            if not isinstance(op, dict):
                continue
            kind = op.get("op")
            try:
                if kind == "add" and op.get("text"):
                    self.add_fact(
                        op["text"], kind=op.get("kind", "otro"), source="auto"
                    )
                    changed += 1
                elif kind == "update" and op.get("id") and op.get("text"):
                    changed += self.update_fact(int(op["id"]), op["text"])
                elif kind == "delete" and op.get("id"):
                    changed += self.delete_fact(int(op["id"]))
            except (TypeError, ValueError):
                continue
        return changed

    def import_legacy(self, facts: List[str], messages: List[Dict[str, Any]]) -> None:
        """One-off import of the old facts list and session history."""
        if self.get_meta("imported"):
            return
        for text in facts:
            try:
                self.add_fact(text, source="usuario")
            except ValueError:
                pass
        for m in messages:
            self.record(m["role"], m["text"], channel="whatsapp", ts=m["ts"])
        self.set_meta("imported", str(time.time()))


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y")


class MemoryKeeper:
    """Background thread: learns facts after each exchange, writes the journal."""

    JOURNAL_EVERY_S = 3600

    def __init__(
        self,
        memory: LongMemory,
        generate: Callable[[str, str, int], str],
        *,
        allowed: Callable[[], bool] = lambda: True,
    ) -> None:
        self._memory = memory
        self._generate = generate  # (system_prompt, user_text, max_tokens) -> text
        self._allowed = allowed
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=200)
        self._thread: Optional[threading.Thread] = None
        self._last_journal = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="memory", daemon=True)
        self._thread.start()

    def submit(self, user_text: str, reply: str) -> None:
        try:
            self._queue.put_nowait((user_text, reply))
        except queue.Full:
            logger.debug("Memory queue full; exchange not analysed")

    def _loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=60)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    self.learn(*item)
                if time.time() - self._last_journal > self.JOURNAL_EVERY_S:
                    self._last_journal = time.time()
                    self.write_journal()
            except Exception:
                logger.debug("Memory worker error", exc_info=True)

    def learn(self, user_text: str, reply: str) -> int:
        if not self._allowed() or len((user_text or "").strip()) < 3:
            return 0
        known = self._memory.relevant_facts(user_text + " " + reply, 40)
        known_txt = "\n".join(f"[{f['id']}] {f['text']}" for f in known) or "(ninguno)"
        out = self._generate(
            EXTRACT_PROMPT,
            f"Recuerdos existentes:\n{known_txt}\n\nIntercambio:\nUsuario: "
            f"{user_text[:3000]}\nAsistente: {reply[:2000]}",
            600,
        )
        return self._memory.apply_ops(_parse_json_list(out))

    def write_journal(self, today: Optional[date] = None) -> int:
        if not self._allowed():
            return 0
        written = 0
        for day in self._memory.days_to_journal(today or date.today())[:7]:
            start = datetime.fromisoformat(day).timestamp()
            end = (datetime.fromisoformat(day) + timedelta(days=1)).timestamp()
            msgs = self._memory.log_between(start, end)
            if not msgs:
                continue
            text = "\n".join(
                f"{'Usuario' if m['role'] == 'user' else 'Asistente'}: {m['text'][:800]}"
                for m in msgs
            )[-30000:]
            summary = self._generate(JOURNAL_PROMPT, f"Día {day}:\n{text}", 500).strip()
            self._memory.set_journal(day, summary or "-")
            written += 1
        return written


# -- shared instance -------------------------------------------------------------

_MEMORY: Optional[LongMemory] = None
_KEEPER: Optional[MemoryKeeper] = None
_LOCK = threading.Lock()


def _home() -> Path:
    from openjarvis.core.paths import get_config_dir

    return Path(os.environ.get("OPENJARVIS_HOME", "") or get_config_dir())


def get_memory() -> LongMemory:
    global _MEMORY
    with _LOCK:
        if _MEMORY is None:
            _MEMORY = LongMemory(_home() / "memory.db")
        return _MEMORY


def set_memory(memory: Optional[LongMemory]) -> None:
    """Replace the shared instance (tests)."""
    global _MEMORY, _KEEPER
    with _LOCK:
        _MEMORY = memory
        _KEEPER = None


def get_keeper() -> Optional[MemoryKeeper]:
    return _KEEPER


def start_keeper(system: Any) -> MemoryKeeper:
    """Start the background learner using the system's engine and model."""
    global _KEEPER
    from openjarvis.core.types import Message, Role

    def generate(system_prompt: str, text: str, max_tokens: int) -> str:
        out = system.engine.generate(
            [
                Message(role=Role.SYSTEM, content=system_prompt),
                Message(role=Role.USER, content=text),
            ],
            model=system.model,
            temperature=0,
            max_tokens=max_tokens,
        )
        return (out or {}).get("content", "") if isinstance(out, dict) else str(out)

    def allowed() -> bool:
        try:
            from openjarvis.fasttrack.credits import get_meter

            return not get_meter().blocked_reply()
        except Exception:
            return True

    memory = get_memory()
    with _LOCK:
        if _KEEPER is None:
            _KEEPER = MemoryKeeper(memory, generate, allowed=allowed)
            _KEEPER.start()
        return _KEEPER


__all__ = [
    "KINDS",
    "LongMemory",
    "MemoryKeeper",
    "get_keeper",
    "get_memory",
    "set_memory",
    "start_keeper",
]
