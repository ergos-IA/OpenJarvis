# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Todoist through the personal API token (Todoist API v1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from openjarvis.integrations.base import (
    Field,
    I,
    Integration,
    IntegrationError,
    S,
    register,
    request,
    tool,
)

API = "https://api.todoist.com/api/v1"
AUTH_ERROR = (
    "Todoist no acepta el token. Cópialo de nuevo desde Todoist → Configuración "
    "→ Integraciones → Desarrollador y vuelve a conectarlo."
)
# In the app «Prioridad 1» is the most urgent; the API counts the other way.
_PRIO_NAME = {4: "urgente", 3: "alta", 2: "media"}


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token.strip()}"}


def _results(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict):
        return data.get("results") or []
    return data or []


def _due(due: Dict[str, Any] | None) -> str:
    if not due:
        return ""
    raw = due.get("datetime") or due.get("date") or ""
    try:
        if "T" in raw:
            text = datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
                "%d/%m/%Y %H:%M"
            )
        else:
            text = datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        text = raw
    if due.get("is_recurring") and due.get("string"):
        text += f" (se repite: {due['string']})"
    return text


def _check(creds: Dict[str, str]) -> str:
    data = request(
        "GET",
        f"{API}/projects",
        headers=_headers(creds["token"]),
        params={"limit": 200},
        auth_error=AUTH_ERROR,
    )
    n = len(_results(data))
    return f"Todoist · {n} proyecto{'s' if n != 1 else ''}"


def _tools(creds: Dict[str, str]):
    h = _headers(creds["token"])

    def get(path: str, **params: Any) -> Any:
        return request(
            "GET", f"{API}{path}", headers=h, params=params, auth_error=AUTH_ERROR
        )

    def projects() -> List[Dict[str, Any]]:
        return _results(get("/projects", limit=200))

    def project_id(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        for p in projects():
            if p.get("id") == value or p.get("name", "").lower() == value.lower():
                return p["id"]
        raise IntegrationError(
            f"No encuentro el proyecto «{value}». Usa todoist_list_projects para ver los nombres."
        )

    @tool(
        "todoist_list_tasks",
        "Lista las tareas pendientes de Todoist. Admite un filtro de Todoist "
        "(p. ej. «today | overdue», «tomorrow», «p1», «#Trabajo») y/o un proyecto.",
        {
            "filtro": S(
                "Filtro de Todoist, en inglés (p. ej. «today | overdue», «7 days»). Vacío = todas"
            ),
            "proyecto": S("Nombre o ID del proyecto (opcional)"),
            "limite": I("Máximo de tareas (1-100)"),
        },
        integration="todoist",
    )
    def todoist_list_tasks(
        filtro: str = "", proyecto: str = "", limite: int = 50
    ) -> Any:
        limit = max(1, min(int(limite or 50), 100))
        names = {p["id"]: p.get("name", "") for p in projects()}
        if filtro:
            query = filtro
            if proyecto:
                query = f"({filtro}) & #{proyecto}"
            data = get("/tasks/filter", query=query, lang="en", limit=limit)
        else:
            params: Dict[str, Any] = {"limit": limit}
            if proyecto:
                params["project_id"] = project_id(proyecto)
            data = get("/tasks", **params)
        tasks = _results(data)
        if not tasks:
            return "No hay tareas pendientes con ese criterio. 🎉"
        out = []
        for t in tasks[:limit]:
            item: Dict[str, Any] = {"id": t["id"], "tarea": t.get("content", "")}
            if fecha := _due(t.get("due")):
                item["fecha"] = fecha
            if prio := _PRIO_NAME.get(t.get("priority") or 1):
                item["prioridad"] = prio
            if proj := names.get(t.get("project_id")):
                item["proyecto"] = proj
            if t.get("description"):
                item["nota"] = t["description"][:200]
            out.append(item)
        return out

    @tool(
        "todoist_add_task",
        "Crea una tarea en Todoist. La fecha se puede dar en lenguaje natural "
        "en español (p. ej. «mañana a las 10», «cada lunes», «el 3 de mayo»).",
        {
            "tarea": S("Texto de la tarea"),
            "fecha": S("Cuándo, en lenguaje natural en español (opcional)"),
            "prioridad": I(
                "1 = urgente, 2 = alta, 3 = media, 4 = normal (por defecto)"
            ),
            "proyecto": S(
                "Nombre o ID del proyecto (opcional; si no, va a la Bandeja de entrada)"
            ),
            "nota": S("Descripción o nota adicional (opcional)"),
        },
        ["tarea"],
        writes=True,
        integration="todoist",
    )
    def todoist_add_task(
        tarea: str,
        fecha: str = "",
        prioridad: int = 4,
        proyecto: str = "",
        nota: str = "",
    ) -> str:
        body: Dict[str, Any] = {"content": tarea}
        if fecha:
            body["due_string"] = fecha
            body["due_lang"] = "es"
        p = int(prioridad or 4)
        if p in (1, 2, 3, 4):
            body["priority"] = 5 - p
        if proyecto:
            body["project_id"] = project_id(proyecto)
        if nota:
            body["description"] = nota
        task = request(
            "POST", f"{API}/tasks", headers=h, json=body, auth_error=AUTH_ERROR
        )
        when = _due((task or {}).get("due"))
        return f"Tarea creada: «{tarea}»" + (f" para el {when}" if when else "") + "."

    @tool(
        "todoist_complete_task",
        "Marca una tarea de Todoist como hecha (usa el ID de todoist_list_tasks).",
        {"id": S("ID de la tarea")},
        ["id"],
        writes=True,
        integration="todoist",
    )
    def todoist_complete_task(id: str) -> str:
        request("POST", f"{API}/tasks/{id}/close", headers=h, auth_error=AUTH_ERROR)
        return "Tarea marcada como hecha. ✅"

    @tool(
        "todoist_list_projects",
        "Lista los proyectos de Todoist.",
        integration="todoist",
    )
    def todoist_list_projects() -> Any:
        items = projects()
        if not items:
            return "No hay proyectos."
        return [
            {"id": p["id"], "nombre": p.get("name", "")}
            | ({"bandeja_de_entrada": True} if p.get("inbox_project") else {})
            for p in items
        ]

    return [
        todoist_list_tasks,
        todoist_add_task,
        todoist_complete_task,
        todoist_list_projects,
    ]


register(
    Integration(
        id="todoist",
        name="Todoist",
        category="productividad",
        description="Apunta, consulta y completa tus tareas de Todoist.",
        abilities=[
            "¿Qué tareas tengo para hoy?",
            "Apúntame llamar al gestor mañana a las 10",
            "Marca como hecha la de pagar el seguro",
        ],
        steps=[
            'Entra en <a href="https://app.todoist.com/app/settings/integrations/developer" target="_blank" rel="noopener">Todoist → Configuración → Integraciones → Desarrollador</a> (inicia sesión si te lo pide).',
            "En el apartado «<b>Token de API</b>» pulsa «<b>Copiar token de API</b>».",
            "Pégalo aquí abajo y pulsa conectar. ¡Listo! Jarvis podrá ver y apuntar tus tareas.",
        ],
        fields=[Field("token", "Token de API", placeholder="0123abcd…")],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas todoist_*: listar tareas (con filtro), crear, completar y listar proyectos.",
        icon="✓",
        color="#E44332",
        link="https://todoist.com",
    )
)
