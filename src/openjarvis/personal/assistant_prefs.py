"""How the user wants their assistant to behave (``<home>/assistant.json``).

Two kinds of settings:

* **live** -- read on every message: when to ask for confirmation, reply
  style, how to address the user, the assistant's name and free-form
  instructions.
* **engine** -- model parameters (creativity, reply length, steps per task).
  ``deploy/railway/bootstrap_config.py`` copies them into ``config.toml`` at
  start-up, so changing them restarts the service.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

CONFIRM_MODES = ("smart", "always", "never")
CONFIRM_LABELS = {
    "smart": "Solo cuando haga falta",
    "always": "Siempre antes de enviar o cambiar algo",
    "never": "Nunca: hazlo directamente",
}
STYLES = ("brief", "normal", "detailed")
ADDRESS = ("tu", "usted")

LIVE_DEFAULTS: Dict[str, Any] = {
    "confirm": "smart",
    "style": "brief",
    "address": "tu",
    "assistant_name": "",
    "instructions": "",
}

# name -> (default, min, max, cast)
ENGINE_FIELDS: Dict[str, tuple] = {
    "temperature": (0.5, 0.0, 1.0, float),
    "max_tokens": (2048, 256, 8192, int),
    "max_turns": (15, 3, 40, int),
}

MAX_INSTRUCTIONS = 2000


def _home() -> Path:
    from openjarvis.core.paths import get_config_dir

    return Path(os.environ.get("OPENJARVIS_HOME", "") or get_config_dir())


def _clamp(name: str, value: Any) -> Any:
    default, lo, hi, cast = ENGINE_FIELDS[name]
    try:
        value = cast(value)
    except (TypeError, ValueError):
        raise ValueError(f"Valor no válido para {name}") from None
    return max(lo, min(hi, value))


class AssistantPrefs:
    """Thread-safe JSON-backed settings.  ``path=None`` keeps them in memory."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        if path is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
            except (OSError, ValueError):
                pass

    # -- reading ------------------------------------------------------------------

    def live(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(LIVE_DEFAULTS)
            out.update(
                {k: v for k, v in self._data.items() if k in LIVE_DEFAULTS and v}
            )
        return out

    def engine(self) -> Dict[str, Any]:
        with self._lock:
            saved = dict(self._data.get("engine") or {})
        out = {}
        for name, (default, *_rest) in ENGINE_FIELDS.items():
            try:
                out[name] = _clamp(name, saved.get(name, default))
            except ValueError:
                out[name] = default
        return out

    def settings(self) -> Dict[str, Any]:
        live = self.live()
        return {
            **live,
            "confirm_label": CONFIRM_LABELS[live["confirm"]],
            "engine": self.engine(),
            "limits": {
                k: {"min": lo, "max": hi, "default": d}
                for k, (d, lo, hi, _c) in ENGINE_FIELDS.items()
            },
        }

    # -- writing ------------------------------------------------------------------

    def update(self, **changes: Any) -> bool:
        """Apply *changes*; returns True if an engine setting changed."""
        live: Dict[str, Any] = {}
        for key in LIVE_DEFAULTS:
            if changes.get(key) is None:
                continue
            value = changes[key]
            if key == "confirm" and value not in CONFIRM_MODES:
                raise ValueError("Modo de confirmación no válido")
            if key == "style" and value not in STYLES:
                raise ValueError("Estilo no válido")
            if key == "address" and value not in ADDRESS:
                raise ValueError("Tratamiento no válido")
            if key == "assistant_name":
                value = str(value).strip()[:40]
            if key == "instructions":
                value = str(value).strip()[:MAX_INSTRUCTIONS]
            live[key] = value
        engine_in = changes.get("engine") or {}
        engine = {k: _clamp(k, v) for k, v in engine_in.items() if k in ENGINE_FIELDS}

        with self._lock:
            before = dict(self._data.get("engine") or {})
            self._data.update(live)
            if engine:
                self._data["engine"] = {**before, **engine}
            after = dict(self._data.get("engine") or {})
            self._save()
        return before != after

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self._path)

    # -- prompt -------------------------------------------------------------------

    def prompt_rules(self) -> str:
        """Behaviour rules for the system prompt, from the live settings."""
        s = self.live()
        lines = []
        if s["confirm"] == "always":
            lines.append(
                "Confirmaciones: antes de enviar un mensaje o correo, o de crear, "
                "cambiar o borrar algo en sus servicios, enséñale lo que vas a "
                "hacer y espera su «sí»."
            )
        elif s["confirm"] == "never":
            lines.append(
                "Confirmaciones: el usuario NO quiere que le pidas confirmación. "
                "Si te pide algo claro, hazlo directamente y cuéntale lo que has "
                "hecho. Pregunta solo si hay ambigüedad real (p. ej. dos contactos "
                "posibles) o si vas a borrar algo de forma irreversible."
            )
        else:
            lines.append(
                "Confirmaciones: no pidas confirmación para lo que el usuario te "
                "ha pedido de forma clara (escribir a un contacto concreto, crear "
                "una tarea, un evento o una nota): hazlo y cuéntale lo hecho. "
                "Pregunta antes solo si falta un dato, si hay ambigüedad, si vas "
                "a borrar algo o si la acción es delicada (pagos, enviar a mucha "
                "gente, algo que el usuario no ha pedido expresamente)."
            )
        lines.append(
            {
                "brief": "Estilo: respuestas muy breves y directas.",
                "normal": "Estilo: respuestas claras, con el detalle justo.",
                "detailed": "Estilo: respuestas completas y detalladas.",
            }[s["style"]]
        )
        if s["address"] == "usted":
            lines.append("Trata al usuario de usted.")
        if s["instructions"]:
            lines.append("Instrucciones personales del usuario:\n" + s["instructions"])
        return "\n".join(lines)


_PREFS: Optional[AssistantPrefs] = None
_PREFS_LOCK = threading.Lock()


def get_prefs() -> AssistantPrefs:
    global _PREFS
    with _PREFS_LOCK:
        if _PREFS is None:
            _PREFS = AssistantPrefs(_home() / "assistant.json")
        return _PREFS


def set_prefs(prefs: Optional[AssistantPrefs]) -> None:
    """Replace the shared instance (tests)."""
    global _PREFS
    with _PREFS_LOCK:
        _PREFS = prefs


__all__ = [
    "AssistantPrefs",
    "CONFIRM_MODES",
    "ENGINE_FIELDS",
    "get_prefs",
    "set_prefs",
]
