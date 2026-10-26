# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Oura ring: sleep, readiness and activity (API v2).

Oura deprecated personal access tokens, so this connects with OAuth
(``OPENJARVIS_OURA_CLIENT_ID`` / ``_SECRET`` from developer.ouraring.com).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Tuple

from openjarvis.integrations.base import (
    I,
    Integration,
    OAuthSpec,
    register,
    request,
    tool,
)

API = "https://api.ouraring.com/v2/usercollection"
AUTH_ERROR = "El acceso a Oura ha caducado o se ha retirado. Vuelve a conectarlo."
_DAYS = "lun mar mié jue vie sáb dom".split()


def _token(creds: Dict[str, Any]) -> str:
    getter = getattr(creds, "token", None)
    if callable(getter):
        return getter()
    return str(creds.get("access_token") or creds.get("token") or "")


def _today() -> date:
    from openjarvis.personal.reminders import local_tz

    return datetime.now(local_tz()).date()


def _range(dias: Any) -> Tuple[date, date]:
    try:
        n = max(1, min(int(dias or 7), 31))
    except (TypeError, ValueError):
        n = 7
    today = _today()
    return today - timedelta(days=n - 1), today


def _hm(seconds: Any) -> str:
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def _label(day: str) -> str:
    try:
        d = date.fromisoformat(day)
    except (TypeError, ValueError):
        return str(day)
    return f"{_DAYS[d.weekday()]} {d:%d/%m}"


def _check(creds: Dict[str, Any]) -> str:
    info = request(
        "GET",
        f"{API}/personal_info",
        headers={"Authorization": f"Bearer {_token(creds)}"},
        auth_error=AUTH_ERROR,
    )
    return (info or {}).get("email") or "Anillo Oura"


def _tools(creds: Dict[str, Any]):
    def get(endpoint: str, start: date, end: date) -> List[Dict[str, Any]]:
        data = request(
            "GET",
            f"{API}/{endpoint}",
            headers={"Authorization": f"Bearer {_token(creds)}"},
            # end_date + 1: some collections treat the end as exclusive.
            params={
                "start_date": start.isoformat(),
                "end_date": (end + timedelta(days=1)).isoformat(),
            },
            auth_error=AUTH_ERROR,
        )
        rows = (data or {}).get("data") or []
        return [
            r
            for r in rows
            if start.isoformat() <= str(r.get("day", "")) <= end.isoformat()
        ]

    @tool(
        "oura_sleep",
        "Resumen del sueño de los últimos días según el anillo Oura: puntuación, "
        "horas dormidas, eficiencia, sueño profundo/REM y pulso en reposo.",
        {"dias": I("Cuántos días hacia atrás (1-31, por defecto 7; 1 = anoche)")},
        integration="oura",
    )
    def oura_sleep(dias: int = 7) -> str:
        start, end = _range(dias)
        scores = {r["day"]: r.get("score") for r in get("daily_sleep", start, end)}
        main: Dict[str, Dict[str, Any]] = {}
        for s in get("sleep", start, end):
            day = s.get("day")
            if not day:
                continue
            best = main.get(day)
            # Keep the longest sleep of the day (the night, not naps).
            if best is None or (s.get("total_sleep_duration") or 0) > (
                best.get("total_sleep_duration") or 0
            ):
                main[day] = s
        days = sorted(set(scores) | set(main), reverse=True)
        if not days:
            return "Oura no tiene datos de sueño de esos días (¿has sincronizado el anillo con la app?)."
        lines = ["Sueño (Oura):"]
        for day in days:
            s = main.get(day, {})
            parts = []
            if scores.get(day) is not None:
                parts.append(f"puntuación {scores[day]}")
            if s.get("total_sleep_duration"):
                parts.append(f"dormido {_hm(s['total_sleep_duration'])}")
            if s.get("efficiency") is not None:
                parts.append(f"eficiencia {s['efficiency']}%")
            if s.get("deep_sleep_duration"):
                parts.append(f"profundo {_hm(s['deep_sleep_duration'])}")
            if s.get("rem_sleep_duration"):
                parts.append(f"REM {_hm(s['rem_sleep_duration'])}")
            if s.get("lowest_heart_rate"):
                parts.append(f"pulso mín. {s['lowest_heart_rate']} ppm")
            if s.get("bedtime_start") and s.get("bedtime_end"):
                parts.append(
                    f"de {s['bedtime_start'][11:16]} a {s['bedtime_end'][11:16]}"
                )
            lines.append(f"- {_label(day)}: " + ", ".join(parts))
        return "\n".join(lines)

    @tool(
        "oura_readiness",
        "Nivel de recuperación (readiness) de los últimos días según Oura: "
        "puntuación y variación de temperatura corporal.",
        {"dias": I("Cuántos días hacia atrás (1-31, por defecto 7; 1 = hoy)")},
        integration="oura",
    )
    def oura_readiness(dias: int = 7) -> str:
        start, end = _range(dias)
        rows = sorted(
            get("daily_readiness", start, end), key=lambda r: r["day"], reverse=True
        )
        if not rows:
            return "Oura no tiene datos de recuperación de esos días."
        lines = ["Recuperación (Oura, 0-100; 85+ es óptima):"]
        for r in rows:
            parts = [f"puntuación {r.get('score')}"]
            temp = r.get("temperature_deviation")
            if temp is not None:
                parts.append(
                    f"temperatura {temp:+.1f} °C respecto a lo habitual".replace(
                        ".", ","
                    )
                )
            lines.append(f"- {_label(r['day'])}: " + ", ".join(parts))
        return "\n".join(lines)

    @tool(
        "oura_activity",
        "Actividad diaria según Oura: pasos, calorías activas y totales, "
        "distancia y puntuación de actividad.",
        {"dias": I("Cuántos días hacia atrás (1-31, por defecto 7; 1 = hoy)")},
        integration="oura",
    )
    def oura_activity(dias: int = 7) -> str:
        start, end = _range(dias)
        rows = sorted(
            get("daily_activity", start, end), key=lambda r: r["day"], reverse=True
        )
        if not rows:
            return "Oura no tiene datos de actividad de esos días."
        lines = ["Actividad (Oura):"]
        for r in rows:
            parts = [f"{int(r.get('steps') or 0):,} pasos".replace(",", ".")]
            if r.get("active_calories") is not None:
                parts.append(f"{r['active_calories']} kcal activas")
            if r.get("total_calories") is not None:
                parts.append(f"{r['total_calories']} kcal en total")
            if r.get("equivalent_walking_distance"):
                parts.append(
                    f"{r['equivalent_walking_distance'] / 1000:.1f} km".replace(
                        ".", ","
                    )
                )
            if r.get("score") is not None:
                parts.append(f"puntuación {r['score']}")
            lines.append(f"- {_label(r['day'])}: " + ", ".join(parts))
        return "\n".join(lines)

    return [oura_sleep, oura_readiness, oura_activity]


register(
    Integration(
        id="oura",
        name="Oura",
        category="personal",
        description="Tu sueño, recuperación y actividad del anillo Oura.",
        abilities=[
            "¿Qué tal he dormido esta semana?",
            "¿Cómo tengo hoy la recuperación?",
            "¿Cuántos pasos di ayer?",
        ],
        steps=[
            "Pulsa el botón «<b>Conectar</b>».",
            "Se abrirá la web de Oura: entra con tu cuenta de Oura (la misma de la app del anillo).",
            "Deja marcados los permisos que te pide (resúmenes diarios, datos personales y email) y pulsa «<b>Accept</b>» (Aceptar).",
            "Listo: volverás aquí y Jarvis ya podrá consultar tus datos. Necesitas tener la membresía de Oura activa.",
        ],
        oauth=OAuthSpec(
            provider="oura",
            authorize_url="https://cloud.ouraring.com/oauth/authorize",
            token_url="https://api.ouraring.com/oauth/token",
            scopes=["email", "personal", "daily"],
            client_env="OURA",
        ),
        check=_check,
        build_tools=_tools,
        prompt="Herramientas oura_*: sueño, recuperación (readiness) y actividad de los últimos días.",
        icon="💍",
        color="#a6b1c2",
        link="https://ouraring.com",
    )
)
