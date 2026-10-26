# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Google (Calendar, Drive, Contacts, Tasks) through OAuth.

Gmail is not here: Jarvis reads mail with an app password elsewhere.
Event ids from ``google_calendar_events`` are ``<calendar>::<event>`` when the
event is not in the primary calendar, so update/delete find it again.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, List, Tuple

from openjarvis.integrations.base import (
    B,
    I,
    Integration,
    IntegrationError,
    OAuthSpec,
    S,
    pick,
    register,
    request,
    tool,
)

CAL = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
PEOPLE = "https://people.googleapis.com/v1"
TASKS = "https://tasks.googleapis.com/tasks/v1"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
    # calendarList.list does not accept calendar.events; this read-only scope
    # lets Jarvis see every calendar the user has (work, family, holidays...).
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/tasks",
]

AUTH_ERROR = (
    "Google no me deja entrar: el acceso ha caducado o falta algún permiso. "
    "Vuelve a conectar Google desde la página de conectores."
)
MAX_TEXT = 5000
_DAYS = "lun mar mié jue vie sáb dom".split()
_ID_SEP = "::"


def _tz() -> tzinfo:
    from openjarvis.personal.reminders import local_tz

    return local_tz()


def _api(creds: Any, method: str, url: str, **kwargs: Any) -> Any:
    headers = {"Authorization": f"Bearer {creds.token()}", **kwargs.pop("headers", {})}
    return request(method, url, headers=headers, auth_error=AUTH_ERROR, **kwargs)


# -- dates -------------------------------------------------------------------


def _parse_dt(text: str) -> Tuple[datetime, bool]:
    """Local 'AAAA-MM-DD HH:MM' (or ISO) -> aware datetime, all_day flag."""
    value = (text or "").strip().replace("T", " ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=_tz()), True
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise IntegrationError(
            f"Fecha no válida «{text}»: usa AAAA-MM-DD HH:MM (o AAAA-MM-DD para todo el día)."
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt, False


def _parse_day(text: str) -> date:
    if not text:
        return datetime.now(_tz()).date()
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        raise IntegrationError("Fecha no válida: usa AAAA-MM-DD.")


def _when(ev: Dict[str, Any]) -> str:
    tz = _tz()
    start, end = ev.get("start") or {}, ev.get("end") or {}
    if "date" in start:
        d = date.fromisoformat(start["date"])
        return f"{_DAYS[d.weekday()]} {d:%d/%m} (todo el día)"
    s = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(tz)
    text = f"{_DAYS[s.weekday()]} {s:%d/%m %H:%M}"
    if end.get("dateTime"):
        e = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00")).astimezone(
            tz
        )
        text += f"-{e:%H:%M}" if e.date() == s.date() else f" → {e:%d/%m %H:%M}"
    return text


def _sort_key(ev: Dict[str, Any]) -> str:
    start = ev.get("start") or {}
    if "date" in start:
        return start["date"] + "T00:00:00"
    dt = datetime.fromisoformat(start.get("dateTime", "").replace("Z", "+00:00"))
    return dt.astimezone(_tz()).strftime("%Y-%m-%dT%H:%M:%S")


def _event_body(
    title: str,
    start: str,
    end: str = "",
    duration: int = 0,
    location: str = "",
    description: str = "",
    attendees: str = "",
) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if title:
        body["summary"] = title
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if attendees:
        emails = [a for a in re.split(r"[\s,;]+", attendees) if "@" in a]
        body["attendees"] = [{"email": a} for a in emails]
    if start:
        s, all_day = _parse_dt(start)
        if all_day:
            e = _parse_dt(end)[0] if end else s
            body["start"] = {"date": s.date().isoformat()}
            # Google's all-day end date is exclusive.
            body["end"] = {"date": (e.date() + timedelta(days=1)).isoformat()}
        else:
            if end:
                e = _parse_dt(end)[0]
            else:
                e = s + timedelta(minutes=int(duration or 60))
            if e <= s:
                raise IntegrationError("La hora de fin debe ser posterior al inicio.")
            zone = getattr(_tz(), "key", None)
            body["start"] = {
                "dateTime": s.isoformat(),
                **({"timeZone": zone} if zone else {}),
            }
            body["end"] = {
                "dateTime": e.isoformat(),
                **({"timeZone": zone} if zone else {}),
            }
    return body


def _split_id(event_id: str, calendar: str = "") -> Tuple[str, str]:
    if _ID_SEP in event_id:
        cal, _, ev = event_id.partition(_ID_SEP)
        return cal, ev
    return calendar or "primary", event_id


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


# -- check / tools -------------------------------------------------------------


def _check(creds: Any) -> str:
    me = _api(creds, "GET", USERINFO) or {}
    return me.get("email") or me.get("name") or "Google"


def _tools(creds: Any):
    def calendars() -> List[Dict[str, Any]]:
        try:
            data = _api(
                creds,
                "GET",
                f"{CAL}/users/me/calendarList",
                params={"minAccessRole": "reader", "maxResults": 50},
            )
        except IntegrationError:
            return [{"id": "primary", "primary": True}]
        cals = [c for c in data.get("items", []) if not c.get("hidden")]
        return cals or [{"id": "primary", "primary": True}]

    @tool(
        "google_calendar_events",
        "Lista los eventos de Google Calendar del usuario (todos sus calendarios) "
        "entre dos fechas; por defecto hoy y los próximos 7 días. Opcionalmente "
        "busca un texto. Devuelve el id de cada evento (para cambiarlo o borrarlo).",
        {
            "desde": S("Fecha inicial AAAA-MM-DD (por defecto hoy)"),
            "dias": I("Número de días a mostrar (1-60, por defecto 7)"),
            "buscar": S("Texto a buscar (título, lugar, asistentes...)"),
        },
        integration="google",
    )
    def google_calendar_events(desde: str = "", dias: int = 7, buscar: str = "") -> Any:
        first = _parse_day(desde)
        start = datetime(first.year, first.month, first.day, tzinfo=_tz())
        end = start + timedelta(days=max(1, min(int(dias or 7), 60)))
        found: List[Tuple[str, Dict[str, Any]]] = []
        for cal in calendars()[:15]:
            params: Dict[str, Any] = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 100,
            }
            if buscar:
                params["q"] = buscar
            cal_id = "primary" if cal.get("primary") else cal["id"]
            try:
                data = _api(
                    creds, "GET", f"{CAL}/calendars/{cal_id}/events", params=params
                )
            except IntegrationError:
                if cal_id == "primary":
                    raise
                continue
            for ev in data.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                ev["_cal"] = "" if cal_id == "primary" else cal_id
                ev["_calname"] = "" if cal_id == "primary" else cal.get("summary", "")
                found.append((_sort_key(ev), ev))
        if not found:
            return "No hay eventos en ese periodo."
        found.sort(key=lambda x: x[0])
        out = []
        for _, ev in found[:80]:
            item = {
                "id": f"{ev['_cal']}{_ID_SEP}{ev['id']}" if ev["_cal"] else ev["id"],
                "cuando": _when(ev),
                "titulo": ev.get("summary") or "(sin título)",
                "lugar": ev.get("location", ""),
                "calendario": ev["_calname"],
                "enlace_videollamada": ev.get("hangoutLink", ""),
                "asistentes": ", ".join(
                    a.get("email", "") for a in ev.get("attendees", [])[:8]
                ),
            }
            out.append(
                pick(
                    item,
                    "id",
                    "cuando",
                    "titulo",
                    "lugar",
                    "calendario",
                    "enlace_videollamada",
                    "asistentes",
                )
            )
        return out

    @tool(
        "google_calendar_create_event",
        "Crea un evento en el calendario principal de Google del usuario. Si se "
        "indican invitados, NO se les envía invitación salvo que el usuario lo "
        "pida expresamente (enviar_invitaciones=true).",
        {
            "titulo": S("Título del evento"),
            "inicio": S(
                "Inicio, hora local: AAAA-MM-DD HH:MM (o AAAA-MM-DD si es todo el día)"
            ),
            "fin": S("Fin, hora local AAAA-MM-DD HH:MM (opcional)"),
            "duracion_min": I("Duración en minutos si no hay fin (por defecto 60)"),
            "lugar": S("Lugar (opcional)"),
            "descripcion": S("Notas (opcional)"),
            "invitados": S("Emails de invitados separados por comas (opcional)"),
            "enviar_invitaciones": B(
                "Enviar email de invitación a los invitados (solo si el usuario lo pide)"
            ),
        },
        ["titulo", "inicio"],
        writes=True,
        integration="google",
    )
    def google_calendar_create_event(
        titulo: str,
        inicio: str,
        fin: str = "",
        duracion_min: int = 60,
        lugar: str = "",
        descripcion: str = "",
        invitados: str = "",
        enviar_invitaciones: bool = False,
    ) -> str:
        body = _event_body(
            titulo, inicio, fin, duracion_min, lugar, descripcion, invitados
        )
        ev = _api(
            creds,
            "POST",
            f"{CAL}/calendars/primary/events",
            params={"sendUpdates": "all" if enviar_invitaciones else "none"},
            json=body,
        )
        return f"Evento creado: {_when(ev)} · {ev.get('summary', titulo)} (id {ev.get('id', '')})"

    @tool(
        "google_calendar_update_event",
        "Cambia un evento de Google Calendar (solo los campos indicados). "
        "Usa el id que devuelve google_calendar_events.",
        {
            "id": S("Id del evento (de google_calendar_events)"),
            "titulo": S("Nuevo título"),
            "inicio": S("Nuevo inicio AAAA-MM-DD HH:MM (o AAAA-MM-DD)"),
            "fin": S("Nuevo fin AAAA-MM-DD HH:MM"),
            "duracion_min": I(
                "Nueva duración en minutos (si cambias el inicio sin fin)"
            ),
            "lugar": S("Nuevo lugar"),
            "descripcion": S("Nuevas notas"),
            "enviar_invitaciones": B(
                "Avisar a los invitados del cambio (solo si el usuario lo pide)"
            ),
        },
        ["id"],
        writes=True,
        integration="google",
    )
    def google_calendar_update_event(
        id: str,
        titulo: str = "",
        inicio: str = "",
        fin: str = "",
        duracion_min: int = 0,
        lugar: str = "",
        descripcion: str = "",
        enviar_invitaciones: bool = False,
    ) -> str:
        cal, ev_id = _split_id(id)
        url = f"{CAL}/calendars/{cal}/events/{ev_id}"
        if inicio and not fin and not duracion_min:
            # Keep the current length when only the start moves.
            cur = _api(creds, "GET", url)
            s, e = (cur.get("start") or {}), (cur.get("end") or {})
            if s.get("dateTime") and e.get("dateTime"):
                length = datetime.fromisoformat(
                    e["dateTime"].replace("Z", "+00:00")
                ) - datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00"))
                duracion_min = int(length.total_seconds() // 60)
        if fin and not inicio:
            body = _event_body(titulo, "", "", 0, lugar, descripcion)
            e_dt, all_day = _parse_dt(fin)
            body["end"] = (
                {"date": (e_dt.date() + timedelta(days=1)).isoformat()}
                if all_day
                else {"dateTime": e_dt.isoformat()}
            )
        else:
            body = _event_body(titulo, inicio, fin, duracion_min, lugar, descripcion)
        if not body:
            raise IntegrationError("No me has dicho qué cambiar del evento.")
        ev = _api(
            creds,
            "PATCH",
            url,
            params={"sendUpdates": "all" if enviar_invitaciones else "none"},
            json=body,
        )
        return f"Evento actualizado: {_when(ev)} · {ev.get('summary', '')}"

    @tool(
        "google_calendar_delete_event",
        "Borra un evento de Google Calendar (usa el id de google_calendar_events).",
        {
            "id": S("Id del evento"),
            "avisar_invitados": B(
                "Avisar a los invitados de la cancelación (solo si el usuario lo pide)"
            ),
        },
        ["id"],
        writes=True,
        integration="google",
    )
    def google_calendar_delete_event(id: str, avisar_invitados: bool = False) -> str:
        cal, ev_id = _split_id(id)
        _api(
            creds,
            "DELETE",
            f"{CAL}/calendars/{cal}/events/{ev_id}",
            params={"sendUpdates": "all" if avisar_invitados else "none"},
        )
        return "Evento borrado."

    # -- Drive ------------------------------------------------------------------

    kinds = {
        "application/vnd.google-apps.document": "documento",
        "application/vnd.google-apps.spreadsheet": "hoja de cálculo",
        "application/vnd.google-apps.presentation": "presentación",
        "application/vnd.google-apps.folder": "carpeta",
        "application/vnd.google-apps.form": "formulario",
        "application/pdf": "PDF",
    }

    def kind(mime: str) -> str:
        if mime in kinds:
            return kinds[mime]
        if mime.startswith("image/"):
            return "imagen"
        if mime.startswith("text/"):
            return "texto"
        return mime.rsplit("/", 1)[-1][:40]

    @tool(
        "google_drive_search",
        "Busca archivos en Google Drive por nombre o por su contenido. "
        "Devuelve nombre, tipo, fecha de modificación, enlace e id.",
        {
            "texto": S("Qué buscar (vacío = los más recientes)"),
            "solo_nombre": B(
                "Buscar solo en el nombre del archivo (por defecto también en el contenido)"
            ),
            "limite": I("Máximo de resultados (1-25, por defecto 10)"),
        },
        integration="google",
    )
    def google_drive_search(
        texto: str = "", solo_nombre: bool = False, limite: int = 10
    ) -> Any:
        q = "trashed = false"
        params: Dict[str, Any] = {
            "pageSize": max(1, min(int(limite or 10), 25)),
            "fields": "files(id,name,mimeType,modifiedTime,webViewLink,size)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if texto:
            t = _q(texto)
            cond = f"name contains '{t}'"
            if not solo_nombre:
                cond = f"({cond} or fullText contains '{t}')"
            q += f" and {cond}"
        else:
            params["orderBy"] = "modifiedTime desc"
        params["q"] = q
        data = _api(creds, "GET", f"{DRIVE}/files", params=params)
        files = data.get("files", [])
        if not files:
            return "No he encontrado archivos con eso en Google Drive."
        return [
            pick(
                {
                    "id": f["id"],
                    "nombre": f.get("name", ""),
                    "tipo": kind(f.get("mimeType", "")),
                    "modificado": (f.get("modifiedTime") or "")[:10],
                    "enlace": f.get("webViewLink", ""),
                },
                "id",
                "nombre",
                "tipo",
                "modificado",
                "enlace",
            )
            for f in files
        ]

    @tool(
        "google_drive_read",
        "Lee el texto de un archivo de Google Drive: documentos de Google, hojas "
        "de cálculo (primera hoja, como CSV) y archivos de texto.",
        {"id": S("Id del archivo (de google_drive_search)")},
        ["id"],
        integration="google",
    )
    def google_drive_read(id: str) -> str:
        meta = _api(
            creds,
            "GET",
            f"{DRIVE}/files/{id}",
            params={"fields": "id,name,mimeType,size", "supportsAllDrives": "true"},
        )
        mime = meta.get("mimeType", "")
        name = meta.get("name", "")
        export = {
            "application/vnd.google-apps.document": "text/plain",
            "application/vnd.google-apps.spreadsheet": "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
        }.get(mime)
        if export:
            text = _api(
                creds, "GET", f"{DRIVE}/files/{id}/export", params={"mimeType": export}
            )
        elif mime.startswith("text/") or mime in (
            "application/json",
            "application/xml",
        ):
            if int(meta.get("size") or 0) > 2_000_000:
                return f"«{name}» es demasiado grande para leerlo por aquí."
            text = _api(
                creds,
                "GET",
                f"{DRIVE}/files/{id}",
                params={"alt": "media", "supportsAllDrives": "true"},
            )
        else:
            return (
                f"«{name}» es de tipo {kind(mime)} y no puedo leer su texto. "
                "Puedo leer documentos y hojas de Google y archivos de texto."
            )
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        text = text.strip()
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n… (recortado)"
        return f"# {name}\n{text}" if text else f"«{name}» está vacío."

    # -- Contacts -----------------------------------------------------------------

    @tool(
        "google_contacts_search",
        "Busca en los contactos de Google del usuario por nombre, email o teléfono.",
        {"texto": S("Nombre, email o teléfono a buscar")},
        ["texto"],
        integration="google",
    )
    def google_contacts_search(texto: str) -> Any:
        url = f"{PEOPLE}/people:searchContacts"
        mask = "names,emailAddresses,phoneNumbers,organizations"
        # Google asks for an empty "warm-up" search before the real one.
        _api(creds, "GET", url, params={"query": "", "readMask": mask})
        data = _api(
            creds, "GET", url, params={"query": texto, "readMask": mask, "pageSize": 15}
        )
        out = []
        for r in data.get("results", []):
            p = r.get("person") or {}
            names = p.get("names") or [{}]
            org = (p.get("organizations") or [{}])[0]
            out.append(
                pick(
                    {
                        "nombre": names[0].get("displayName", ""),
                        "emails": ", ".join(
                            e.get("value", "") for e in p.get("emailAddresses", [])
                        ),
                        "telefonos": ", ".join(
                            t.get("value", "") for t in p.get("phoneNumbers", [])
                        ),
                        "empresa": org.get("name", ""),
                    },
                    "nombre",
                    "emails",
                    "telefonos",
                    "empresa",
                )
            )
        return out or f"No encuentro a «{texto}» en tus contactos de Google."

    # -- Tasks ----------------------------------------------------------------------

    def task_list_id(name: str) -> Tuple[str, str]:
        if not name:
            return "@default", ""
        data = _api(
            creds, "GET", f"{TASKS}/users/@me/lists", params={"maxResults": 100}
        )
        lists = data.get("items", [])
        low = name.lower()
        for tl in lists:
            if tl.get("title", "").lower() == low:
                return tl["id"], tl["title"]
        for tl in lists:
            if low in tl.get("title", "").lower():
                return tl["id"], tl["title"]
        names = ", ".join(tl.get("title", "") for tl in lists)
        raise IntegrationError(f"No encuentro la lista «{name}». Tus listas: {names}.")

    @tool(
        "google_tasks_list",
        "Lista las tareas pendientes de Google Tasks (por defecto la lista principal). "
        "Devuelve el id de cada tarea.",
        {
            "lista": S("Nombre de la lista (vacío = la principal)"),
            "incluir_hechas": B("Incluir también las tareas completadas"),
        },
        integration="google",
    )
    def google_tasks_list(lista: str = "", incluir_hechas: bool = False) -> Any:
        list_id, _ = task_list_id(lista)
        params = {
            "maxResults": 100,
            "showCompleted": "true" if incluir_hechas else "false",
            "showHidden": "true" if incluir_hechas else "false",
        }
        data = _api(creds, "GET", f"{TASKS}/lists/{list_id}/tasks", params=params)
        tasks = [t for t in data.get("items", []) if t.get("title")]
        if not tasks:
            return "No hay tareas pendientes en esa lista."
        return [
            pick(
                {
                    "id": t["id"],
                    "tarea": t.get("title", ""),
                    "notas": (t.get("notes") or "")[:200],
                    "fecha": (t.get("due") or "")[:10],
                    "hecha": t.get("status") == "completed" or None,
                },
                "id",
                "tarea",
                "notas",
                "fecha",
                "hecha",
            )
            for t in tasks
        ]

    @tool(
        "google_tasks_add",
        "Añade una tarea a Google Tasks.",
        {
            "titulo": S("La tarea"),
            "notas": S("Detalles (opcional)"),
            "fecha": S("Fecha límite AAAA-MM-DD (opcional)"),
            "lista": S("Nombre de la lista (vacío = la principal)"),
        },
        ["titulo"],
        writes=True,
        integration="google",
    )
    def google_tasks_add(
        titulo: str, notas: str = "", fecha: str = "", lista: str = ""
    ) -> str:
        list_id, list_name = task_list_id(lista)
        body: Dict[str, Any] = {"title": titulo}
        if notas:
            body["notes"] = notas
        if fecha:
            # Google Tasks stores only the date part of "due".
            body["due"] = f"{_parse_day(fecha).isoformat()}T00:00:00.000Z"
        _api(creds, "POST", f"{TASKS}/lists/{list_id}/tasks", json=body)
        where = f" en «{list_name}»" if list_name else ""
        return f"Tarea añadida{where}: {titulo}"

    @tool(
        "google_tasks_complete",
        "Marca como hecha una tarea de Google Tasks (usa el id de google_tasks_list).",
        {
            "id": S("Id de la tarea"),
            "lista": S("Nombre de la lista (vacío = la principal)"),
        },
        ["id"],
        writes=True,
        integration="google",
    )
    def google_tasks_complete(id: str, lista: str = "") -> str:
        list_id, _ = task_list_id(lista)
        t = _api(
            creds,
            "PATCH",
            f"{TASKS}/lists/{list_id}/tasks/{id}",
            json={"status": "completed"},
        )
        return f"Tarea completada: {(t or {}).get('title', '')}".rstrip(": ")

    return [
        google_calendar_events,
        google_calendar_create_event,
        google_calendar_update_event,
        google_calendar_delete_event,
        google_drive_search,
        google_drive_read,
        google_contacts_search,
        google_tasks_list,
        google_tasks_add,
        google_tasks_complete,
    ]


register(
    Integration(
        id="google",
        name="Google (Calendar, Drive, Contactos, Tasks)",
        category="productividad",
        description="Tu agenda, tus documentos, tus contactos y tus tareas de Google.",
        abilities=[
            "Apúntame dentista el jueves a las 17:00",
            "Busca en Drive el presupuesto de la reforma y dime el total",
            "¿Cuál es el teléfono de Marta?",
        ],
        steps=[
            "Pulsa el botón «<b>Conectar con Google</b>».",
            "Elige tu cuenta de Google (la de tu Gmail o la del trabajo).",
            "Google te enseñará qué podrá hacer Jarvis: marca todas las casillas y pulsa «<b>Continuar</b>».",
            "Si aparece el aviso «<b>Google no ha verificado esta aplicación</b>», es normal mientras Google revisa Jarvis: pulsa «<b>Avanzado</b>» (o «Configuración avanzada») y después «<b>Ir a Jarvis</b>».",
            "¡Listo! Volverás aquí y verás tu cuenta conectada. Tu correo sigue funcionando como hasta ahora.",
        ],
        oauth=OAuthSpec(
            provider="google",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
            client_env="GOOGLE",
            extra_params={
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
            },
        ),
        check=_check,
        build_tools=_tools,
        prompt="Herramientas google_*: agenda (google_calendar_events/create/update/delete_event), Drive (google_drive_search/read), contactos (google_contacts_search) y tareas (google_tasks_list/add/complete).",
        icon="G",
        color="#4285F4",
        link="https://myaccount.google.com",
    )
)
