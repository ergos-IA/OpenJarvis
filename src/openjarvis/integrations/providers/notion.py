# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Notion through an internal integration secret pasted by the user."""

from __future__ import annotations

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

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": VERSION,
        "Content-Type": "application/json",
    }


def _title(obj: Dict[str, Any]) -> str:
    if obj.get("object") == "database":
        return (
            "".join(t.get("plain_text", "") for t in obj.get("title", []))
            or "(sin título)"
        )
    for prop in (obj.get("properties") or {}).values():
        if prop.get("type") == "title":
            return (
                "".join(t.get("plain_text", "") for t in prop["title"])
                or "(sin título)"
            )
    return "(sin título)"


def _rich(items: List[Dict[str, Any]]) -> str:
    return "".join(t.get("plain_text", "") for t in items or [])


def _block_text(block: Dict[str, Any]) -> str:
    kind = block.get("type", "")
    data = block.get(kind) or {}
    text = _rich(data.get("rich_text", []))
    prefix = {
        "heading_1": "# ",
        "heading_2": "## ",
        "heading_3": "### ",
        "bulleted_list_item": "- ",
        "numbered_list_item": "1. ",
        "quote": "> ",
    }.get(kind, "")
    if kind == "to_do":
        prefix = "[x] " if data.get("checked") else "[ ] "
    if kind == "child_page":
        return f"(subpágina: {data.get('title', '')})"
    return prefix + text if text else ""


def _prop_value(prop: Dict[str, Any]) -> str:
    kind = prop.get("type")
    v = prop.get(kind)
    if kind in ("title", "rich_text"):
        return _rich(v)
    if kind in ("select", "status"):
        return (v or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(x.get("name", "") for x in v or [])
    if kind == "date":
        return (v or {}).get("start", "") or ""
    if kind in ("number", "checkbox", "url", "email", "phone_number"):
        return "" if v is None else str(v)
    if kind == "people":
        return ", ".join(x.get("name", "") for x in v or [])
    return ""


def _check(creds: Dict[str, str]) -> str:
    me = request("GET", f"{API}/users/me", headers=_headers(creds["token"]))
    bot = (me or {}).get("bot") or {}
    return bot.get("workspace_name") or (me or {}).get("name") or "Notion"


def _tools(creds: Dict[str, str]):
    h = _headers(creds["token"])

    @tool(
        "notion_search",
        "Busca páginas y bases de datos de Notion compartidas con Jarvis.",
        {
            "texto": S("Qué buscar (vacío = lo más reciente)"),
            "limite": I("Máximo de resultados (1-20)"),
        },
        integration="notion",
    )
    def notion_search(texto: str = "", limite: int = 10) -> Any:
        body: Dict[str, Any] = {"page_size": max(1, min(int(limite or 10), 20))}
        if texto:
            body["query"] = texto
        body["sort"] = {"direction": "descending", "timestamp": "last_edited_time"}
        data = request("POST", f"{API}/search", headers=h, json=body)
        results = data.get("results", [])
        if not results:
            return (
                "No he encontrado nada. Recuerda que solo veo las páginas que has "
                "compartido con la integración de Jarvis en Notion."
            )
        return [
            {
                "id": r["id"],
                "tipo": "base de datos" if r["object"] == "database" else "página",
                "titulo": _title(r),
                "editado": r.get("last_edited_time", "")[:10],
                "url": r.get("url", ""),
            }
            for r in results
        ]

    @tool(
        "notion_read",
        "Lee el contenido de una página de Notion, o las filas de una base de datos.",
        {"id": S("ID de la página o base de datos (de notion_search)")},
        ["id"],
        integration="notion",
    )
    def notion_read(id: str) -> str:
        try:
            page = request("GET", f"{API}/pages/{id}", headers=h)
        except IntegrationError:
            page = None
        if page is None:
            rows = request(
                "POST", f"{API}/databases/{id}/query", headers=h, json={"page_size": 50}
            )
            lines = []
            for row in rows.get("results", []):
                props = {
                    k: _prop_value(p) for k, p in (row.get("properties") or {}).items()
                }
                lines.append(" | ".join(f"{k}: {v}" for k, v in props.items() if v))
            return "\n".join(lines) or "La base de datos está vacía."
        props = "\n".join(
            f"{k}: {v}"
            for k, p in (page.get("properties") or {}).items()
            if (v := _prop_value(p)) and p.get("type") != "title"
        )
        blocks = request(
            "GET", f"{API}/blocks/{id}/children", headers=h, params={"page_size": 100}
        )
        body = "\n".join(t for b in blocks.get("results", []) if (t := _block_text(b)))
        return f"# {_title(page)}\n{props}\n\n{body}".strip()

    @tool(
        "notion_add_to_page",
        "Añade texto al final de una página de Notion (una línea por párrafo; "
        "las líneas que empiezan por «- » se añaden como lista y «[ ] » como tarea).",
        {"id": S("ID de la página"), "texto": S("Texto a añadir")},
        ["id", "texto"],
        writes=True,
        integration="notion",
    )
    def notion_add_to_page(id: str, texto: str) -> str:
        children = []
        for line in texto.splitlines():
            if not line.strip():
                continue
            kind, content, extra = "paragraph", line, {}
            if line.startswith("- "):
                kind, content = "bulleted_list_item", line[2:]
            elif line.startswith("[ ] "):
                kind, content, extra = "to_do", line[4:], {"checked": False}
            children.append(
                {
                    "object": "block",
                    "type": kind,
                    kind: {
                        "rich_text": [
                            {"type": "text", "text": {"content": content[:1900]}}
                        ],
                        **extra,
                    },
                }
            )
        if not children:
            raise IntegrationError("No hay texto que añadir.")
        request(
            "PATCH",
            f"{API}/blocks/{id}/children",
            headers=h,
            json={"children": children[:100]},
        )
        return f"Añadido a la página ({len(children)} bloques)."

    @tool(
        "notion_create_page",
        "Crea una página nueva dentro de otra página de Notion (o una fila nueva "
        "en una base de datos, usando su título).",
        {
            "padre_id": S("ID de la página o base de datos donde crearla"),
            "titulo": S("Título"),
            "texto": S("Contenido inicial (opcional)"),
        },
        ["padre_id", "titulo"],
        writes=True,
        integration="notion",
    )
    def notion_create_page(padre_id: str, titulo: str, texto: str = "") -> str:
        title = [{"type": "text", "text": {"content": titulo[:200]}}]
        try:
            db = request("GET", f"{API}/databases/{padre_id}", headers=h)
        except IntegrationError:
            db = None
        if db:
            title_prop = next(
                (
                    k
                    for k, p in db.get("properties", {}).items()
                    if p.get("type") == "title"
                ),
                "Name",
            )
            body: Dict[str, Any] = {
                "parent": {"database_id": padre_id},
                "properties": {title_prop: {"title": title}},
            }
        else:
            body = {
                "parent": {"page_id": padre_id},
                "properties": {"title": {"title": title}},
            }
        if texto:
            body["children"] = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": p[:1900]}}]
                    },
                }
                for p in texto.split("\n")
                if p.strip()
            ][:100]
        page = request("POST", f"{API}/pages", headers=h, json=body)
        return f"Página creada: {page.get('url', '')}"

    return [notion_search, notion_read, notion_add_to_page, notion_create_page]


register(
    Integration(
        id="notion",
        name="Notion",
        category="productividad",
        description="Busca, lee y añade notas en tus páginas y bases de datos.",
        abilities=[
            "Busca en Notion el acta del lunes",
            "Apunta esto en mi página de ideas",
            "¿Qué tareas hay en el tablero?",
        ],
        steps=[
            'Entra en <a href="https://www.notion.so/profile/integrations" target="_blank" rel="noopener">notion.so/profile/integrations</a> y pulsa «<b>Nueva integración</b>».',
            "Ponle de nombre «Jarvis», elige tu espacio de trabajo, tipo «Interna», y guarda.",
            "Copia el «<b>Secreto de integración interna</b>» (empieza por ntn_ o secret_) y pégalo aquí.",
            "Importante: en Notion, abre cada página que quieras que Jarvis vea → «⋯» arriba a la derecha → <b>Conexiones</b> → añade «Jarvis». Las subpáginas se incluyen solas.",
        ],
        fields=[Field("token", "Secreto de integración", placeholder="ntn_…")],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas notion_*: buscar, leer, añadir texto y crear páginas.",
        icon="N",
        color="#ffffff",
        link="https://www.notion.so",
    )
)
