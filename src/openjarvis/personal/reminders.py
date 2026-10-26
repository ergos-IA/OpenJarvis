"""Reminders and scheduled tasks, delivered to the user's WhatsApp chat.

A *reminder* sends its text at the given time.  A *task* is an instruction
("resúmeme los correos sin leer") that the assistant carries out at that time
-- through the channel's ``task_runner``, i.e. the same agent and tools as a
normal chat -- and whose answer is sent to the user.

Times are local (``TZ``, default Europe/Madrid).  Repeats are ``daily``,
``weekdays``, ``weekly``, ``monthly`` or a 5-field cron expression.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, tzinfo
from typing import Any, Callable, Dict, List, Optional

from openjarvis.personal.store import PersonalStore

logger = logging.getLogger(__name__)

_POLL_S = 30
# Per-instance limits: tasks run the AI, so keep them bounded.
MAX_ACTIVE_TASKS = 10
MAX_ACTIVE_REMINDERS = 100
MIN_TASK_INTERVAL_S = 30 * 60
MAX_TASK_RUNS_PER_DAY = 48
REPEATS = ("daily", "weekdays", "weekly", "monthly")
_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def local_tz() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(os.environ.get("TZ") or "Europe/Madrid")
    except Exception:
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def now_text() -> str:
    """Current local date and time, for the assistant's instructions."""
    now = datetime.now(local_tz())
    return f"{_DAYS[now.weekday()]} {now:%d/%m/%Y %H:%M}"


def parse_when(value: str) -> float:
    """Local ``YYYY-MM-DD HH:MM`` (or ISO) -> epoch seconds."""
    text = (value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(
                "Fecha no válida; usa el formato AAAA-MM-DD HH:MM"
            ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz())
    return parsed.timestamp()


def repeat_to_cron(repeat: str, first_run: float) -> str:
    """Translate a repeat keyword (anchored at *first_run*) to cron."""
    repeat = (repeat or "").strip().lower()
    if not repeat or repeat in ("no", "none", "once"):
        return ""
    first = datetime.fromtimestamp(first_run, local_tz())
    m, h = first.minute, first.hour
    if repeat == "daily":
        return f"{m} {h} * * *"
    if repeat == "weekdays":
        return f"{m} {h} * * 1-5"
    if repeat == "weekly":
        return f"{m} {h} * * {(first.weekday() + 1) % 7}"
    if repeat == "monthly":
        return f"{m} {h} {first.day} * *"
    from croniter import croniter

    if len(repeat.split()) == 5 and croniter.is_valid(repeat):
        return repeat
    raise ValueError(
        "Repetición no válida: usa daily, weekdays, weekly, monthly o una "
        "expresión cron"
    )


def next_cron_run(cron: str, after: float) -> float:
    from croniter import croniter

    base = datetime.fromtimestamp(after, local_tz())
    return croniter(cron, base).get_next(datetime).timestamp()


def describe_cron(cron: str) -> str:
    parts = cron.split()
    if len(parts) != 5:
        return cron
    m, h, dom, _mon, dow = parts
    at = f"{int(h):02d}:{int(m):02d}" if h.isdigit() and m.isdigit() else ""
    if dom == "*" and dow == "*":
        return f"cada día a las {at}"
    if dom == "*" and dow == "1-5":
        return f"de lunes a viernes a las {at}"
    if dom == "*" and dow.isdigit():
        return f"cada {_DAYS[(int(dow) - 1) % 7]} a las {at}"
    if dom.isdigit() and dow == "*":
        return f"el día {dom} de cada mes a las {at}"
    return f"cron «{cron}»"


def format_when(ts: float) -> str:
    when = datetime.fromtimestamp(ts, local_tz())
    return f"{_DAYS[when.weekday()]} {when:%d/%m %H:%M}"


class ReminderService:
    """Creates reminders and delivers them when due (background poller)."""

    def __init__(
        self,
        store: Optional[PersonalStore] = None,
        *,
        notifier: Optional[Callable[[str], bool]] = None,
        task_runner: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._task_runner = task_runner
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._run_lock = threading.Lock()

    @property
    def store(self) -> PersonalStore:
        if self._store is None:
            self._store = PersonalStore()
        return self._store

    # -- API used by the tools --------------------------------------------

    def create(
        self, text: str, when: str, *, repeat: str = "", is_task: bool = False
    ) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("Falta el texto del recordatorio")
        first = parse_when(when)
        cron = repeat_to_cron(repeat, first)
        self._check_limits(is_task, cron, first)
        if first < time.time() - 60:
            if not cron:
                raise ValueError(
                    f"Esa hora ya ha pasado ({format_when(first)}); pide otra"
                )
            first = next_cron_run(cron, time.time())
        rid = self.store.add_reminder(text, first, cron=cron, is_task=is_task)
        return self.store.get_reminder(rid) or {}

    def _check_limits(self, is_task: bool, cron: str, first: float) -> None:
        active = self.store.active_reminders()
        if is_task:
            if sum(1 for r in active if r["is_task"]) >= MAX_ACTIVE_TASKS:
                raise ValueError(
                    f"Ya tienes {MAX_ACTIVE_TASKS} tareas programadas activas; "
                    "cancela alguna antes de crear otra"
                )
            if cron:
                start = max(first, time.time())
                runs = [start]
                while len(runs) <= MAX_TASK_RUNS_PER_DAY + 1:
                    runs.append(next_cron_run(cron, runs[-1]))
                    if runs[-1] - start > 24 * 3600:
                        break
                gaps = [b - a for a, b in zip(runs[1:], runs[2:])]
                if gaps and min(gaps) < MIN_TASK_INTERVAL_S:
                    raise ValueError(
                        "Una tarea programada no puede repetirse más de una vez "
                        "cada 30 minutos"
                    )
                per_day = sum(1 for t in runs[1:] if t - start <= 24 * 3600)
                if per_day > MAX_TASK_RUNS_PER_DAY:
                    raise ValueError(
                        "Una tarea no puede ejecutarse más de "
                        f"{MAX_TASK_RUNS_PER_DAY} veces al día"
                    )
        elif sum(1 for r in active if not r["is_task"]) >= MAX_ACTIVE_REMINDERS:
            raise ValueError(
                f"Has llegado al máximo de {MAX_ACTIVE_REMINDERS} recordatorios activos"
            )

    def cancel(self, reminder_id: int) -> bool:
        item = self.store.get_reminder(reminder_id)
        if item is None or item["status"] != "active":
            return False
        self.store.update_reminder(reminder_id, status="cancelled")
        return True

    def active(self) -> List[Dict[str, Any]]:
        return self.store.active_reminders()

    # -- delivery ---------------------------------------------------------

    def run_due(self, now: Optional[float] = None) -> int:
        """Deliver every due reminder; return how many were delivered."""
        now = time.time() if now is None else now
        notify = self._notifier or _notify_whatsapp
        delivered = 0
        with self._run_lock:
            for item in self.store.due_reminders(now):
                text = self._render(item, now)
                if text is None or not notify(text):
                    # WhatsApp not linked/connected yet: try again later.
                    continue
                delivered += 1
                if item["cron"]:
                    self.store.update_reminder(
                        item["id"],
                        next_run=next_cron_run(
                            item["cron"], max(now, item["next_run"])
                        ),
                        last_run=now,
                    )
                else:
                    self.store.update_reminder(item["id"], status="done", last_run=now)
        return delivered

    def _render(self, item: Dict[str, Any], now: float) -> Optional[str]:
        late = ""
        if now - item["next_run"] > 15 * 60:
            late = f" _(era para el {format_when(item['next_run'])})_"
        if not item["is_task"]:
            return f"⏰ *Recordatorio*{late}: {item['text']}"
        runner = self._task_runner or _channel_task_runner()
        if runner is None:
            return None
        try:
            from openjarvis.fasttrack.credits import get_meter

            if get_meter().blocked_reply():
                return (
                    f"🗓️ *{item['text']}*{late}\n\n"
                    "No la he hecho: sin créditos de IA este mes."
                )
        except Exception:
            pass
        try:
            result = runner(
                "Tarea programada por el usuario; hazla ahora y responde con el "
                f"resultado para enviárselo: {item['text']}"
            )
        except Exception as exc:
            logger.exception("Scheduled task %s failed", item["id"])
            result = f"No he podido hacerla: {exc}"
        return f"🗓️ *{item['text']}*{late}\n\n{(result or '').strip()}"

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="reminders", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(_POLL_S):
            try:
                self.run_due()
            except Exception:
                logger.exception("Reminder poller iteration failed")


def _active_channel() -> Any:
    from openjarvis.channels.whatsapp_baileys import get_active_channel

    return get_active_channel()


def _notify_whatsapp(text: str) -> bool:
    channel = _active_channel()
    own = channel.self_jid() if channel is not None else ""
    return bool(own) and bool(channel.send(own, text))


def _channel_task_runner() -> Optional[Callable[[str], str]]:
    channel = _active_channel()
    return getattr(channel, "task_runner", None) if channel is not None else None


_service: Optional[ReminderService] = None
_service_lock = threading.Lock()


def get_reminder_service() -> ReminderService:
    global _service
    with _service_lock:
        if _service is None:
            _service = ReminderService()
        return _service


__all__ = [
    "REPEATS",
    "ReminderService",
    "describe_cron",
    "format_when",
    "get_reminder_service",
    "now_text",
    "parse_when",
    "repeat_to_cron",
]
