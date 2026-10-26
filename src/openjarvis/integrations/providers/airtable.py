# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Airtable through a personal access token pasted by the user."""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import quote

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

API = "https://api.airtable.com/v0"
AUTH_ERROR = (
    "Airtable no acepta ese token o no tiene permiso para esa base. Revisa los "
    "permisos del token y que la base está añadida en «Access»."
)


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _fields_arg(value: Any) -> Dict[str, Any]:
    """The model may send the fields as an object or as a JSON string."""
    if isinstance(value, dict):
        fields = value
    else:
        try:
            fields = json.loads(value or "{}")
        except (TypeError, ValueError):
            raise IntegrationError(
                'Los campos deben ir como objeto, p. ej. {"Nombre": "Ana", "Teléfono": "600…"}.'
            )
    if not isinstance(fields, dict) or not fields:
        raise IntegrationError("No hay campos que guardar.")
    return fields


def _value(v: Any) -> Any:
    """Compact a cell: attachments/links/collaborators become names or URLs."""
    if isinstance(v, list):
        out = [_value(x) for x in v]
        return ", ".join(str(x) for x in out if x not in (None, ""))
    if isinstance(v, dict):
        return (
            v.get("name") or v.get("email") or v.get("filename") or v.get("url") or ""
        )
    if isinstance(v, str) and len(v) > 400:
        return v[:400] + "…"
    return v


def _record(r: Dict[str, Any]) -> Dict[str, Any]:
    fields = {k: _value(v) for k, v in (r.get("fields") or {}).items()}
    return {
        "id": r.get("id"),
        **{k: v for k, v in fields.items() if v not in (None, "", [])},
    }


def _formula_text(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _check(creds: Dict[str, str]) -> str:
    me = request(
        "GET",
        f"{API}/meta/whoami",
        headers=_headers(creds["token"]),
        auth_error=AUTH_ERROR,
    )
    return (me or {}).get("email") or "Airtable"


def _tools(creds: Dict[str, str]):
    h = _headers(creds["token"])

    def table_url(base: str, table: str) -> str:
        return f"{API}/{quote(base, safe='')}/{quote(table, safe='')}"

    def schema(base: str) -> List[Dict[str, Any]]:
        data = request(
            "GET",
            f"{API}/meta/bases/{quote(base, safe='')}/tables",
            headers=h,
            auth_error=AUTH_ERROR,
        )
        return (data or {}).get("tables", [])

    @tool(
        "airtable_list_bases",
        "Lista las bases de Airtable a las que Jarvis tiene acceso.",
        integration="airtable",
    )
    def airtable_list_bases() -> Any:
        data = request("GET", f"{API}/meta/bases", headers=h, auth_error=AUTH_ERROR)
        bases = (data or {}).get("bases", [])
        if not bases:
            return (
                "No veo ninguna base. En airtable.com/create/tokens, edita el token "
                "de Jarvis y añade tus bases en «Access»."
            )
        return [{"id": b.get("id"), "nombre": b.get("name")} for b in bases]

    @tool(
        "airtable_list_tables",
        "Lista las tablas de una base de Airtable con sus campos (columnas).",
        {"base_id": S("ID de la base (empieza por app…, de airtable_list_bases)")},
        ["base_id"],
        integration="airtable",
    )
    def airtable_list_tables(base_id: str) -> Any:
        tables = schema(base_id)
        if not tables:
            return "Esa base no tiene tablas."
        return [
            {
                "id": t.get("id"),
                "tabla": t.get("name"),
                "campos": ", ".join(
                    f"{f.get('name')} ({f.get('type')})" for f in t.get("fields", [])
                ),
            }
            for t in tables
        ]

    @tool(
        "airtable_search_records",
        "Busca o lista registros (filas) de una tabla de Airtable. Puede buscar un "
        "texto en todos los campos o usar una fórmula de Airtable (filterByFormula).",
        {
            "base_id": S("ID de la base (app…)"),
            "tabla": S("Nombre o ID de la tabla"),
            "texto": S("Texto a buscar en cualquier campo (opcional)"),
            "formula": S(
                "Fórmula de filtro de Airtable, p. ej. {Estado}='Pendiente' (opcional, avanzada)"
            ),
            "vista": S(
                "Nombre de una vista para respetar su filtro y orden (opcional)"
            ),
            "limite": I("Máximo de registros (1-100, por defecto 20)"),
        },
        ["base_id", "tabla"],
        integration="airtable",
    )
    def airtable_search_records(
        base_id: str,
        tabla: str,
        texto: str = "",
        formula: str = "",
        vista: str = "",
        limite: int = 20,
    ) -> Any:
        params: Dict[str, Any] = {"maxRecords": max(1, min(int(limite or 20), 100))}
        if formula:
            params["filterByFormula"] = formula
        elif texto:
            table = next(
                (t for t in schema(base_id) if tabla in (t.get("id"), t.get("name"))),
                None,
            )
            if table is None:
                raise IntegrationError(f"No encuentro la tabla «{tabla}» en esa base.")
            # Field names cannot contain "}" inside {…} references.
            names = [
                f["name"].replace("}", "")
                for f in table.get("fields", [])
                if f.get("name")
            ]
            needle = _formula_text(texto.lower())
            parts = [f"FIND({needle}, LOWER({{{n}}}&''))" for n in names[:40]]
            params["filterByFormula"] = f"OR({', '.join(parts)})" if parts else ""
        if vista:
            params["view"] = vista
        data = request(
            "GET",
            table_url(base_id, tabla),
            headers=h,
            params=params,
            auth_error=AUTH_ERROR,
        )
        records = (data or {}).get("records", [])
        if not records:
            return "No hay registros que coincidan."
        return [_record(r) for r in records]

    @tool(
        "airtable_create_record",
        "Crea un registro (fila) nuevo en una tabla de Airtable.",
        {
            "base_id": S("ID de la base (app…)"),
            "tabla": S("Nombre o ID de la tabla"),
            "campos": {
                "type": "object",
                "description": 'Valores por nombre de campo, p. ej. {"Nombre": "Ana", "Estado": "Pendiente"}',
            },
        },
        ["base_id", "tabla", "campos"],
        writes=True,
        integration="airtable",
    )
    def airtable_create_record(base_id: str, tabla: str, campos: Any) -> str:
        body = {"records": [{"fields": _fields_arg(campos)}], "typecast": True}
        data = request(
            "POST",
            table_url(base_id, tabla),
            headers=h,
            json=body,
            auth_error=AUTH_ERROR,
        )
        rec = ((data or {}).get("records") or [{}])[0]
        return f"Registro creado en «{tabla}» (id {rec.get('id', '?')})."

    @tool(
        "airtable_update_record",
        "Cambia campos de un registro existente de Airtable (solo los indicados).",
        {
            "base_id": S("ID de la base (app…)"),
            "tabla": S("Nombre o ID de la tabla"),
            "registro_id": S("ID del registro (rec…, de airtable_search_records)"),
            "campos": {
                "type": "object",
                "description": 'Campos a cambiar, p. ej. {"Estado": "Hecho"}',
            },
        },
        ["base_id", "tabla", "registro_id", "campos"],
        writes=True,
        integration="airtable",
    )
    def airtable_update_record(
        base_id: str, tabla: str, registro_id: str, campos: Any
    ) -> str:
        body = {
            "records": [{"id": registro_id, "fields": _fields_arg(campos)}],
            "typecast": True,
        }
        request(
            "PATCH",
            table_url(base_id, tabla),
            headers=h,
            json=body,
            auth_error=AUTH_ERROR,
        )
        return f"Registro {registro_id} actualizado."

    return [
        airtable_list_bases,
        airtable_list_tables,
        airtable_search_records,
        airtable_create_record,
        airtable_update_record,
    ]


register(
    Integration(
        id="airtable",
        name="Airtable",
        category="negocio",
        description="Consulta y apunta datos en tus tablas de Airtable.",
        abilities=[
            "¿Qué pedidos están pendientes en Airtable?",
            "Añade a Marta López a la tabla de clientes",
            "Marca el pedido de Juan como entregado",
        ],
        steps=[
            'Entra en <a href="https://airtable.com/create/tokens" target="_blank" rel="noopener">airtable.com/create/tokens</a> y pulsa «<b>Create token</b>» (Crear token).',
            "Ponle de nombre «Jarvis».",
            "En «<b>Scopes</b>» (permisos) pulsa «Add a scope» y añade estos tres: <b>data.records:read</b>, <b>data.records:write</b> y <b>schema.bases:read</b>.",
            "En «<b>Access</b>» (acceso) pulsa «Add a base» y elige las bases que quieras que Jarvis vea (o «All current and future bases»).",
            "Pulsa «<b>Create token</b>», copia el token (empieza por <b>pat</b>) y pégalo aquí. Solo se muestra una vez.",
        ],
        fields=[Field("token", "Token de acceso personal", placeholder="pat…")],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas airtable_*: ver bases y tablas, buscar registros, crear y actualizar registros.",
        icon="▦",
        color="#fcb400",
        link="https://airtable.com",
    )
)
