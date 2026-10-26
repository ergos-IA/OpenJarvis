# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Granola meeting notes through the public API (key pasted by the user)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from openjarvis.integrations.base import (
    Field,
    I,
    Integration,
    IntegrationError,
    S,
    pick,
    register,
    request,
    tool,
)

API = "https://public-api.granola.ai/v1"
AUTH_ERROR = (
    "Granola no acepta esa API key. Revisa que la has copiado entera y que tu "
    "plan de Granola incluye el acceso por API."
)
TRANSCRIPT_CHARS = 2500


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _when(iso: Optional[str]) -> str:
    if not iso:
        return ""
    from openjarvis.personal.reminders import local_tz

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:16]
    days = "lun mar mié jue vie sáb dom".split()
    dt = dt.astimezone(local_tz())
    return f"{days[dt.weekday()]} {dt:%d/%m/%Y %H:%M}"


def _summary_row(n: Dict[str, Any]) -> Dict[str, Any]:
    row = {
        "id": n.get("id"),
        "titulo": n.get("title") or "(sin título)",
        "fecha": _when(n.get("created_at")),
        "de": (n.get("owner") or {}).get("name") or (n.get("owner") or {}).get("email"),
    }
    return pick(row, *row.keys())


def _speaker(turn: Dict[str, Any]) -> str:
    sp = turn.get("speaker")
    if isinstance(sp, dict):
        if sp.get("name"):
            return sp["name"]
        if sp.get("diarization_label"):
            return sp["diarization_label"]
        who = sp.get("attribution") or sp.get("source")
        return {
            "me": "Yo",
            "microphone": "Yo",
            "them": "Otros",
            "speaker": "Otros",
        }.get(who or "", who or "?")
    return str(sp or "?")


def _transcript_excerpt(turns: List[Dict[str, Any]]) -> str:
    out, size = [], 0
    for t in turns or []:
        line = f"{_speaker(t)}: {(t.get('text') or '').strip()}"
        if size + len(line) > TRANSCRIPT_CHARS:
            out.append("… (transcripción recortada)")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def _check(creds: Dict[str, str]) -> str:
    data = request(
        "GET",
        f"{API}/notes",
        headers=_headers(creds["token"]),
        params={"page_size": 1},
        auth_error=AUTH_ERROR,
    )
    notes = (data or {}).get("notes") or []
    if notes:
        owner = notes[0].get("owner") or {}
        return owner.get("email") or owner.get("name") or "Granola"
    return "Granola"


def _tools(creds: Dict[str, str]):
    h = _headers(creds["token"])

    def list_notes(max_notes: int, created_after: str = "") -> List[Dict[str, Any]]:
        notes: List[Dict[str, Any]] = []
        cursor = None
        while len(notes) < max_notes:
            params: Dict[str, Any] = {"page_size": min(30, max_notes - len(notes))}
            if created_after:
                params["created_after"] = created_after
            if cursor:
                params["cursor"] = cursor
            data = request(
                "GET", f"{API}/notes", headers=h, params=params, auth_error=AUTH_ERROR
            )
            notes.extend((data or {}).get("notes") or [])
            cursor = (data or {}).get("cursor")
            if not (data or {}).get("hasMore") or not cursor:
                break
        return [n for n in notes if not n.get("deleted_at")][:max_notes]

    def get_note(note_id: str, transcript: bool = False) -> Dict[str, Any]:
        params = {"include": "transcript"} if transcript else {}
        note = request(
            "GET",
            f"{API}/notes/{note_id}",
            headers=h,
            params=params,
            ok_status=(200, 413) if transcript else (200,),
            auth_error=AUTH_ERROR,
        )
        if transcript and not (isinstance(note, dict) and "id" in note):
            # 413: transcript too large to return inline; get the note alone.
            note = get_note(note_id)
            note["_transcript_too_large"] = True
        if not isinstance(note, dict):
            raise IntegrationError("No he podido leer esa nota.")
        return note

    @tool(
        "granola_list_notes",
        "Lista las últimas reuniones/notas de Granola (más recientes primero).",
        {
            "dias": I("Solo las de los últimos N días (opcional)"),
            "limite": I("Máximo de notas (1-30, por defecto 10)"),
        },
        integration="granola",
    )
    def granola_list_notes(dias: int = 0, limite: int = 10) -> Any:
        after = ""
        if dias:
            after = (datetime.now(timezone.utc) - timedelta(days=int(dias))).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        notes = list_notes(max(1, min(int(limite or 10), 30)), after)
        if not notes:
            return "No hay notas de reuniones en ese periodo."
        return [_summary_row(n) for n in notes]

    @tool(
        "granola_search_notes",
        "Busca reuniones de Granola por título, asistentes o contenido del resumen.",
        {
            "texto": S("Qué buscar, p. ej. «presupuesto» o el nombre de un cliente"),
            "limite": I("Máximo de resultados (1-20, por defecto 5)"),
        },
        ["texto"],
        integration="granola",
    )
    def granola_search_notes(texto: str, limite: int = 5) -> Any:
        q = (texto or "").lower().strip()
        if not q:
            raise IntegrationError("Dime qué quieres buscar.")
        limit = max(1, min(int(limite or 5), 20))
        notes = list_notes(90)
        hits = [
            n
            for n in notes
            if q in (n.get("title") or "").lower()
            or q in str((n.get("owner") or {}).get("name") or "").lower()
        ]
        # Too few title matches: look inside the most recent notes' summaries.
        if len(hits) < limit:
            seen = {n["id"] for n in hits}
            for n in [n for n in notes if n["id"] not in seen][:10]:
                full = get_note(n["id"])
                haystack = " ".join(
                    [
                        full.get("summary_text") or "",
                        " ".join(
                            f"{a.get('name') or ''} {a.get('email') or ''}"
                            for a in full.get("attendees") or []
                        ),
                        ((full.get("calendar_event") or {}).get("event_title") or ""),
                    ]
                ).lower()
                if q in haystack:
                    hits.append(n)
                if len(hits) >= limit:
                    break
        if not hits:
            return f"No encuentro reuniones sobre «{texto}» entre las últimas 90 notas."
        return [_summary_row(n) for n in hits[:limit]]

    @tool(
        "granola_read_note",
        "Lee una nota de reunión de Granola: asistentes, resumen y un extracto de la transcripción.",
        {
            "id": S(
                "ID de la nota (empieza por not_, de granola_list_notes o granola_search_notes)"
            ),
            "transcripcion": {
                "type": "boolean",
                "description": "Incluir un extracto de la transcripción (por defecto sí)",
            },
        },
        ["id"],
        integration="granola",
    )
    def granola_read_note(id: str, transcripcion: bool = True) -> str:
        note = get_note(id, transcript=bool(transcripcion))
        lines = [
            f"# {note.get('title') or '(sin título)'}",
            _when(note.get("created_at")),
        ]
        people = [
            a.get("name") or a.get("email") for a in note.get("attendees") or [] if a
        ]
        if people:
            lines.append("Asistentes: " + ", ".join(p for p in people if p))
        summary = note.get("summary_markdown") or note.get("summary_text") or ""
        lines.append("\n## Resumen\n" + (summary.strip() or "(sin resumen)"))
        if transcripcion:
            if note.get("_transcript_too_large"):
                lines.append(
                    "\n(La transcripción es demasiado larga para mostrarla aquí.)"
                )
            elif note.get("transcript"):
                lines.append(
                    "\n## Transcripción (extracto)\n"
                    + _transcript_excerpt(note["transcript"])
                )
        if note.get("web_url"):
            lines.append(f"\nAbrir en Granola: {note['web_url']}")
        return "\n".join(x for x in lines if x is not None).strip()

    return [granola_list_notes, granola_search_notes, granola_read_note]


register(
    Integration(
        id="granola",
        name="Granola",
        category="productividad",
        description="Consulta las notas y resúmenes de tus reuniones.",
        abilities=[
            "¿Qué se habló en la reunión de ayer con Ana?",
            "Busca la reunión donde hablamos del presupuesto",
            "Resúmeme mis últimas reuniones",
        ],
        steps=[
            "Abre la aplicación de <b>Granola</b> en tu ordenador (la API necesita el plan <b>Business</b> o Enterprise).",
            "Ve a <b>Settings</b> (Ajustes) → <b>Connectors</b> → <b>API keys</b>.",
            "Pulsa «<b>Create new key</b>», deja marcados los permisos de lectura de notas y pulsa «<b>Generate API Key</b>».",
            "Copia la clave (empieza por <b>grn_</b>) y pégala aquí.",
            'Si usas Enterprise y no ves la opción, pide a tu administrador que active «API access for members». Más info en <a href="https://docs.granola.ai" target="_blank" rel="noopener">docs.granola.ai</a>.',
        ],
        fields=[Field("token", "API key de Granola", placeholder="grn_…")],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas granola_*: listar, buscar y leer notas de reuniones (solo lectura).",
        icon="🥣",
        color="#7cb342",
        link="https://www.granola.ai",
    )
)
