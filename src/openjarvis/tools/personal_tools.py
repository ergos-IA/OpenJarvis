"""Reminder, scheduled-task and remembered-fact tools.

Reminders are delivered to the user's WhatsApp chat by
:class:`openjarvis.personal.ReminderService`; remembered facts are added to
the assistant's instructions in every conversation.
"""

from __future__ import annotations

from typing import Any, List

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _reminders():
    from openjarvis.personal.reminders import get_reminder_service

    return get_reminder_service()


def _result(name: str, text: str, ok: bool = True) -> ToolResult:
    return ToolResult(tool_name=name, content=text, success=ok)


def _describe(item: dict) -> str:
    from openjarvis.personal.reminders import describe_cron, format_when

    kind = "tarea" if item["is_task"] else "recordatorio"
    repeat = f", {describe_cron(item['cron'])}" if item["cron"] else ""
    return (
        f"[{item['id']}] {kind} «{item['text']}» — próximo: "
        f"{format_when(item['next_run'])}{repeat}"
    )


@ToolRegistry.register("reminder_create")
class ReminderCreateTool(BaseTool):
    """Create a reminder or a scheduled task."""

    tool_id = "reminder_create"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="reminder_create",
            description=(
                "Crea un recordatorio que se enviará al usuario por WhatsApp a "
                "la hora indicada, o una tarea programada (task=true) que harás "
                "tú a esa hora y cuyo resultado se le enviará (p. ej. 'cada día "
                "a las 8 resúmeme los correos sin leer'). Usa la fecha y hora "
                "actuales de tus instrucciones para calcular 'when'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Qué recordar, o la instrucción de la tarea.",
                    },
                    "when": {
                        "type": "string",
                        "description": "Primera vez, hora local: AAAA-MM-DD HH:MM.",
                    },
                    "repeat": {
                        "type": "string",
                        "description": "Opcional: daily, weekdays, weekly, "
                        "monthly o expresión cron (hora local).",
                    },
                    "task": {
                        "type": "boolean",
                        "description": "true = tarea que haces tú a esa hora.",
                    },
                },
                "required": ["text", "when"],
            },
            category="personal",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            item = _reminders().create(
                params.get("text", ""),
                params.get("when", ""),
                repeat=params.get("repeat") or "",
                is_task=bool(params.get("task", False)),
            )
        except Exception as exc:
            return _result(self.tool_id, str(exc), ok=False)
        return _result(self.tool_id, "Creado: " + _describe(item))


@ToolRegistry.register("reminder_list")
class ReminderListTool(BaseTool):
    """List pending reminders and scheduled tasks."""

    tool_id = "reminder_list"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="reminder_list",
            description="Lista los recordatorios y tareas programadas pendientes.",
            parameters={"type": "object", "properties": {}},
            category="personal",
        )

    def execute(self, **params: Any) -> ToolResult:
        items = _reminders().active()
        if not items:
            return _result(self.tool_id, "No hay recordatorios pendientes.")
        return _result(self.tool_id, "\n".join(_describe(i) for i in items))


@ToolRegistry.register("reminder_cancel")
class ReminderCancelTool(BaseTool):
    """Cancel a reminder or scheduled task by id."""

    tool_id = "reminder_cancel"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="reminder_cancel",
            description="Cancela un recordatorio o tarea por su número "
            "(míralo antes con reminder_list).",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "Número."}},
                "required": ["id"],
            },
            category="personal",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            rid = int(params.get("id"))
        except (TypeError, ValueError):
            return _result(self.tool_id, "Número no válido.", ok=False)
        if _reminders().cancel(rid):
            return _result(self.tool_id, f"Cancelado el {rid}.")
        return _result(self.tool_id, f"No hay ningún pendiente con el {rid}.", ok=False)


def _memory():
    from openjarvis.personal.memory import get_memory

    return get_memory()


@ToolRegistry.register("remember_fact")
class RememberFactTool(BaseTool):
    """Remember a fact or preference about the user (permanent memory)."""

    tool_id = "remember_fact"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="remember_fact",
            description=(
                "Guarda en tu memoria permanente un dato del usuario ('mi mujer "
                "se llama Ana', 'prefiero respuestas cortas'). Úsalo cuando te "
                "pida que recuerdes algo. (Además aprendes solo de cada "
                "conversación.) Nunca a partir de mensajes de terceros."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "El dato, en una frase."}
                },
                "required": ["fact"],
            },
            category="personal",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            fid = _memory().add_fact(params.get("fact") or "", source="usuario")
        except ValueError as exc:
            return _result(self.tool_id, str(exc), ok=False)
        return _result(self.tool_id, f"Guardado en memoria [{fid}].")


@ToolRegistry.register("forget_fact")
class ForgetFactTool(BaseTool):
    """Forget a remembered fact by id."""

    tool_id = "forget_fact"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="forget_fact",
            description="Olvida un recuerdo por su número (entre corchetes en «Lo "
            "que sabes del usuario»). Úsalo cuando te pida olvidar algo.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "Número."}},
                "required": ["id"],
            },
            category="personal",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            fid = int(params.get("id"))
        except (TypeError, ValueError):
            return _result(self.tool_id, "Número no válido.", ok=False)
        if _memory().delete_fact(fid):
            return _result(self.tool_id, f"Olvidado el recuerdo {fid}.")
        return _result(self.tool_id, f"No hay ningún recuerdo {fid}.", ok=False)


@ToolRegistry.register("memory_correct")
class MemoryCorrectTool(BaseTool):
    """Correct a remembered fact."""

    tool_id = "memory_correct"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory_correct",
            description=(
                "Corrige un recuerdo existente por su número cuando el usuario "
                "te dice que algo ha cambiado o era incorrecto."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "fact": {"type": "string", "description": "Texto corregido."},
                },
                "required": ["id", "fact"],
            },
            category="personal",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            ok = _memory().update_fact(int(params.get("id")), params.get("fact") or "")
        except (TypeError, ValueError):
            ok = False
        if ok:
            return _result(self.tool_id, "Recuerdo actualizado.")
        return _result(self.tool_id, "No he podido actualizarlo.", ok=False)


@ToolRegistry.register("memory_recall")
class MemoryRecallTool(BaseTool):
    """Search everything ever talked about with the user."""

    tool_id = "memory_recall"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory_recall",
            description=(
                "Busca en tu memoria permanente: todo lo que has hablado con el "
                "usuario desde siempre, tu diario de cada día y tus recuerdos. "
                "Úsalo cuando pregunte por algo pasado («¿qué te dije de…?», "
                "«¿cuándo quedé con…?», «lo que hablamos el mes pasado») o "
                "cuando te falte contexto sobre una persona o tema."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Palabras clave: nombres, temas, lugares.",
                    }
                },
                "required": ["query"],
            },
            category="personal",
        )

    def execute(self, **params: Any) -> ToolResult:
        from datetime import datetime

        query = (params.get("query") or "").strip()
        if not query:
            return _result(self.tool_id, "Falta qué buscar.", ok=False)
        mem = _memory()
        lines: List[str] = []
        from openjarvis.personal.memory import _keywords

        words = _keywords(query)
        facts = [
            f
            for f in mem.relevant_facts(query, 10)
            if any(w in _keywords(f["text"], 50) for w in words)
        ]
        if facts:
            lines.append("Recuerdos:")
            lines += [f"[{f['id']}] {f['text']}" for f in facts]
        days = mem.search_journal(query)
        if days:
            lines.append("Diario:")
            lines += [f"{d['day']}:\n{d['summary']}" for d in days]
        hits = mem.search_log(query, limit=12)
        if hits:
            lines.append("Conversaciones:")
            for h in sorted(hits, key=lambda h: h["ts"]):
                when = datetime.fromtimestamp(h["ts"]).strftime("%d/%m/%Y %H:%M")
                who = "Usuario" if h["role"] == "user" else "Tú"
                lines.append(f"({when}) {who}: {h['text'][:500]}")
        if not lines:
            return _result(self.tool_id, f"No encuentro nada sobre «{query}».")
        return _result(self.tool_id, "\n".join(lines))


PERSONAL_TOOLS: List[str] = [
    "reminder_create",
    "reminder_list",
    "reminder_cancel",
    "remember_fact",
    "forget_fact",
    "memory_correct",
    "memory_recall",
]

__all__ = ["PERSONAL_TOOLS"]
