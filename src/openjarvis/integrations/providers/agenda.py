# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Calendar (read-only) from the secret iCal link of Google, Outlook or iCloud.

No app registration: the user pastes their calendar's private address.
Recurring events are expanded with ``dateutil.rrule``.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, List, Optional, Tuple

from dateutil import rrule

from openjarvis.integrations.base import (
    Field,
    I,
    Integration,
    IntegrationError,
    S,
    fetch_public,
    register,
    tool,
)

# Outlook exports Windows time-zone names.
_WINDOWS_TZ = {
    "Romance Standard Time": "Europe/Madrid",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central European Standard Time": "Europe/Warsaw",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Canary Standard Time": "Atlantic/Canary",
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "Eastern Standard Time": "America/New_York",
    "Pacific Standard Time": "America/Los_Angeles",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "SA Pacific Standard Time": "America/Bogota",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
}

_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_CACHE_S = 300


def _local_tz() -> tzinfo:
    from openjarvis.personal.reminders import local_tz

    return local_tz()


def _zone(name: str) -> tzinfo:
    from zoneinfo import ZoneInfo

    name = name.strip('"')
    try:
        return ZoneInfo(_WINDOWS_TZ.get(name, name))
    except Exception:
        return _local_tz()


def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split(line: str) -> Tuple[str, Dict[str, str], str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    opts = {}
    for p in params:
        k, _, v = p.partition("=")
        opts[k.upper()] = v
    return name.upper(), opts, value


def _text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _when(value: str, opts: Dict[str, str]) -> Tuple[datetime, bool]:
    """Aware datetime and whether it is an all-day date."""
    value = value.strip()
    if opts.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=_local_tz()), True
    if value.endswith("Z"):
        from datetime import timezone

        dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return dt, False
    tz = _zone(opts["TZID"]) if "TZID" in opts else _local_tz()
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz), False


def _duration(value: str) -> timedelta:
    m = re.fullmatch(
        r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
        value.strip(),
    )
    if not m:
        return timedelta(hours=1)
    sign, w, d, h, mi, s = m.groups()
    delta = timedelta(
        weeks=int(w or 0),
        days=int(d or 0),
        hours=int(h or 0),
        minutes=int(mi or 0),
        seconds=int(s or 0),
    )
    return -delta if sign == "-" else delta


def parse_ics(text: str) -> List[Dict[str, Any]]:
    """VEVENTs as dicts: start, end, all_day, summary, location, ... rrule."""
    if "BEGIN:VCALENDAR" not in text[:2000]:
        raise IntegrationError("Ese enlace no es un calendario (iCal).")
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": []}
            continue
        if line == "END:VEVENT" and cur is not None:
            if "start" in cur and cur.get("status") != "CANCELLED":
                if "end" not in cur:
                    cur["end"] = cur["start"] + cur.pop(
                        "duration",
                        (timedelta(days=1) if cur["all_day"] else timedelta(hours=1)),
                    )
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        name, opts, value = _split(line)
        try:
            if name == "DTSTART":
                cur["start"], cur["all_day"] = _when(value, opts)
            elif name == "DTEND":
                cur["end"], _ = _when(value, opts)
            elif name == "DURATION":
                cur["duration"] = _duration(value)
            elif name in ("SUMMARY", "LOCATION", "DESCRIPTION"):
                cur[name.lower()] = _text(value)
            elif name == "RRULE":
                cur["rrule"] = value
            elif name == "EXDATE":
                for v in value.split(","):
                    cur["exdates"].append(_when(v, opts)[0])
            elif name == "RECURRENCE-ID":
                cur["recurrence_id"], _ = _when(value, opts)
            elif name == "UID":
                cur["uid"] = value
            elif name == "STATUS":
                cur["status"] = value.upper()
        except (ValueError, KeyError):
            continue
    return events


def expand(
    events: List[Dict[str, Any]], start: datetime, end: datetime
) -> List[Dict[str, Any]]:
    """Occurrences overlapping [start, end), sorted."""
    overrides = {
        (e.get("uid"), e["recurrence_id"].timestamp())
        for e in events
        if e.get("recurrence_id")
    }
    out = []
    for ev in events:
        length = ev["end"] - ev["start"]
        if not ev.get("rrule") or ev.get("recurrence_id"):
            if ev["start"] < end and ev["end"] > start:
                out.append({**ev, "occurrence": ev["start"]})
            continue
        try:
            rule = rrule.rrulestr(ev["rrule"], dtstart=ev["start"])
        except (ValueError, TypeError):
            continue
        skip = {d.timestamp() for d in ev["exdates"]}
        for occ in rule.between(start - length, end, inc=True):
            if occ.timestamp() in skip or (ev.get("uid"), occ.timestamp()) in overrides:
                continue
            if occ < end and occ + length > start:
                out.append({**ev, "occurrence": occ, "end": occ + length})
    out.sort(key=lambda e: e["occurrence"])
    return out


def _fmt(ev: Dict[str, Any]) -> str:
    tz = _local_tz()
    s = ev["occurrence"].astimezone(tz)
    days = "lun mar mié jue vie sáb dom".split()
    if ev.get("all_day"):
        when = f"{days[s.weekday()]} {s:%d/%m} (todo el día)"
    else:
        e = ev["end"].astimezone(tz)
        when = f"{days[s.weekday()]} {s:%d/%m %H:%M}-{e:%H:%M}"
    line = f"{when} · {ev.get('summary') or '(sin título)'}"
    if ev.get("location"):
        line += f" · {ev['location']}"
    return line


def _events(url: str) -> List[Dict[str, Any]]:
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < _CACHE_S:
        return hit[1]
    events = parse_ics(fetch_public(url))
    _CACHE[url] = (time.time(), events)
    return events


def _urls(creds: Dict[str, str]) -> List[str]:
    urls = [u.strip() for u in re.split(r"[\s,]+", creds.get("urls", "")) if u.strip()]
    return [re.sub(r"^webcals?://", "https://", u, flags=re.I) for u in urls]


def _parse_day(text: Optional[str]) -> date:
    if not text:
        return datetime.now(_local_tz()).date()
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        raise IntegrationError("Fecha no válida: usa AAAA-MM-DD.")


def _check(creds: Dict[str, str]) -> str:
    urls = _urls(creds)
    if not urls:
        raise IntegrationError("Pega al menos un enlace de calendario.")
    total = 0
    for url in urls[:5]:
        _CACHE.pop(url, None)
        total += len(_events(url))
    return f"{len(urls)} calendario(s), {total} eventos"


def _tools(creds: Dict[str, str]):
    urls = _urls(creds)[:5]

    @tool(
        "agenda_events",
        "Lista los eventos del calendario del usuario entre dos fechas "
        "(por defecto, hoy y los próximos 7 días). Opcionalmente filtra por texto.",
        {
            "desde": S("Fecha inicial AAAA-MM-DD (por defecto hoy)"),
            "dias": I("Número de días a mostrar (1-60, por defecto 7)"),
            "buscar": S("Texto a buscar en título, lugar o descripción"),
        },
        integration="agenda",
    )
    def agenda_events(desde: str = "", dias: int = 7, buscar: str = "") -> str:
        first = _parse_day(desde)
        tz = _local_tz()
        start = datetime(first.year, first.month, first.day, tzinfo=tz)
        end = start + timedelta(days=max(1, min(int(dias or 7), 60)))
        found: List[Dict[str, Any]] = []
        for url in urls:
            found.extend(expand(_events(url), start, end))
        if buscar:
            q = buscar.lower()
            found = [
                e
                for e in found
                if q
                in " ".join(
                    str(e.get(k, "")) for k in ("summary", "location", "description")
                ).lower()
            ]
        found.sort(key=lambda e: e["occurrence"])
        if not found:
            return "No hay eventos en ese periodo."
        return "\n".join(_fmt(e) for e in found[:80])

    return [agenda_events]


register(
    Integration(
        id="agenda",
        name="Calendario (Google, Outlook, iCloud)",
        category="productividad",
        description="Tu agenda en modo lectura, sin dar acceso a tu cuenta.",
        abilities=[
            "¿Qué tengo mañana?",
            "¿Cuándo es la reunión con Ana?",
            "Resumen de la semana",
        ],
        steps=[
            "<b>Google Calendar</b>: en el ordenador, abre calendar.google.com → ⚙️ Configuración → en la izquierda, elige tu calendario → baja hasta «<b>Dirección secreta en formato iCal</b>» y copia el enlace.",
            "<b>Outlook</b>: outlook.com o Outlook web → ⚙️ → Calendario → Calendarios compartidos → «Publicar un calendario» → elige el calendario y «Puede ver todos los detalles» → Publicar → copia el enlace <b>ICS</b>.",
            "<b>iCloud</b>: en el iPhone, Calendario → Calendarios → (i) junto al calendario → activa «Calendario público» → Compartir enlace (cambia webcal:// por https://).",
            "Pega el enlace aquí. Puedes pegar varios separados por espacios.",
        ],
        fields=[
            Field(
                "urls",
                "Enlace(s) iCal de tu calendario",
                kind="textarea",
                placeholder="https://calendar.google.com/calendar/ical/…/basic.ics",
                help="Es privado: no lo compartas con nadie más.",
            )
        ],
        check=_check,
        build_tools=_tools,
        prompt="Consulta su agenda con agenda_events (solo lectura).",
        icon="📅",
        color="#4285F4",
    )
)
