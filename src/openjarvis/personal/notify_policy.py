"""Which incoming WhatsApp messages Jarvis forwards to the user's own chat.

Modes:

* ``important`` (default): only what matters -- VIP contacts, urgent words,
  someone insisting, or messages the AI judges important.
* ``all``: every message from individual chats.
* ``contacts``: every message from address-book contacts.
* ``none``: never; messages are only stored (the user can ask for them).

Groups are never forwarded.  Settings live in ``<home>/notify.json`` so they
can change at run time (setup page or by asking Jarvis on WhatsApp).
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MODES = ("important", "all", "contacts", "none")
MODE_LABELS = {
    "important": "Solo lo importante",
    "all": "Todos los mensajes",
    "contacts": "Todos los de mis contactos",
    "none": "Ninguno",
}

URGENT_WORDS = [
    "urgente",
    "urgencia",
    "emergencia",
    "importante",
    "asap",
    "cuanto antes",
    "lo antes posible",
    "llamame",
    "me llamas",
    "llamame cuando puedas",
    "no me contestas",
    "contestame",
    "respondeme",
    "te necesito",
    "necesito ayuda",
    "socorro",
    "hospital",
    "accidente",
    "es grave",
    "ha pasado algo",
    "cuando puedas llama",
    "necesito hablar",
]
INSIST_COUNT = 3
INSIST_WINDOW_S = 15 * 60
AI_DAILY_LIMIT = 60


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower()).strip()


def _digits(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch.isdigit())


class NotifyPolicy:
    def __init__(self, path: Optional[Path] = None, default_mode: str = "important"):
        """*path* ``None`` keeps the settings in memory only (tests)."""
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._default = default_mode if default_mode in MODES else "important"
        self._data = self._load()
        self._recent: Dict[str, List[float]] = {}
        self._ai_day = ""
        self._ai_used = 0

    # -- settings -------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        try:
            data = (
                json.loads(self._path.read_text(encoding="utf-8")) if self._path else {}
            )
        except Exception:
            data = {}
        data.setdefault("mode", self._default)
        data.setdefault("vip", [])
        data.setdefault("keywords", [])
        data.setdefault("ai", True)
        if data["mode"] not in MODES:
            data["mode"] = self._default
        return data

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)

    @property
    def mode(self) -> str:
        return self._data["mode"]

    def settings(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "mode_label": MODE_LABELS[self.mode],
            "modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES],
            "vip": list(self._data["vip"]),
            "keywords": list(self._data["keywords"]),
            "ai": bool(self._data["ai"]),
        }

    def update(
        self,
        *,
        mode: Optional[str] = None,
        vip: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        ai: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if mode is not None:
                if mode not in MODES:
                    raise ValueError("Modo de avisos desconocido")
                self._data["mode"] = mode
            if vip is not None:
                self._data["vip"] = _clean_list(vip)
            if keywords is not None:
                self._data["keywords"] = _clean_list(keywords)
            if ai is not None:
                self._data["ai"] = bool(ai)
            self._save()
        return self.settings()

    def add_vip(self, who: str) -> Dict[str, Any]:
        return self.update(vip=self._data["vip"] + [who])

    def remove_vip(self, who: str) -> Dict[str, Any]:
        target = _norm(who)
        return self.update(vip=[v for v in self._data["vip"] if _norm(v) != target])

    # -- decisions ------------------------------------------------------------

    def _is_vip(self, name: str, phone: str) -> bool:
        name_n, phone_d = _norm(name), _digits(phone)
        for v in self._data["vip"]:
            vd = _digits(v)
            if vd and len(vd) >= 6 and phone_d.endswith(vd[-9:]):
                return True
            vn = _norm(v)
            if vn and not vd and name_n and (vn == name_n or vn in name_n.split()):
                return True
        return False

    def _urgent(self, text: str) -> str:
        t = _norm(text)
        for word in URGENT_WORDS + [_norm(k) for k in self._data["keywords"]]:
            if word and re.search(rf"\b{re.escape(word)}\b", t):
                return word
        return ""

    def _insisting(self, key: str, now: float) -> bool:
        times = [t for t in self._recent.get(key, []) if now - t < INSIST_WINDOW_S]
        times.append(now)
        self._recent[key] = times[-10:]
        return len(times) == INSIST_COUNT  # once per burst

    def decide(
        self,
        *,
        name: str,
        phone: str,
        text: str,
        is_contact: bool,
        now: Optional[float] = None,
    ) -> Tuple[str, str]:
        """``("notify"|"skip"|"ask_ai", reason)`` for one incoming message."""
        now = now or time.time()
        key = _digits(phone) or _norm(name)
        insisting = self._insisting(key, now)
        mode = self.mode
        if mode == "none":
            return "skip", ""
        if mode == "all":
            return "notify", ""
        if mode == "contacts":
            return ("notify", "") if is_contact else ("skip", "")
        if self._is_vip(name, phone):
            return "notify", "VIP"
        word = self._urgent(text)
        if word:
            return "notify", "urgente"
        if insisting:
            return "notify", "insiste"
        if self._data["ai"] and len(_norm(text)) >= 12 and self._ai_budget(now):
            return "ask_ai", ""
        return "skip", ""

    def _ai_budget(self, now: float) -> bool:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        with self._lock:
            if day != self._ai_day:
                self._ai_day, self._ai_used = day, 0
            if self._ai_used >= AI_DAILY_LIMIT:
                return False
            self._ai_used += 1
            return True


TRIAGE_PROMPT = (
    "Eres el filtro de avisos de un asistente personal. Decide si este mensaje "
    "de WhatsApp que ha recibido el usuario merece avisarle AHORA. Responde SOLO "
    "«SI» o «NO». Es SI si: pide algo con plazo cercano, es una cita o cambio de "
    "planes de hoy/mañana, un problema, dinero/pagos, salud, trabajo urgente o "
    "alguien espera respuesta pronto. Es NO si: saludos, charla, memes, "
    "agradecimientos, cadenas, publicidad, o puede esperar."
)


def _clean_list(items: List[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        item = " ".join(str(item).split())[:60]
        if item and _norm(item) not in {_norm(x) for x in out}:
            out.append(item)
    return out[:100]


__all__ = [
    "MODES",
    "MODE_LABELS",
    "NotifyPolicy",
    "TRIAGE_PROMPT",
]
