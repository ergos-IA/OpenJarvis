# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Strava: the user's own activities and totals (read-only) through OAuth."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

from openjarvis.integrations.base import (
    I,
    Integration,
    OAuthSpec,
    pick,
    register,
    request,
    tool,
)

API = "https://www.strava.com/api/v3"
AUTH_ERROR = (
    "Strava no me deja entrar: el acceso ha caducado o se retiró el permiso. "
    "Vuelve a conectar Strava desde la página de conectores."
)

_TYPES = {
    "Run": "carrera",
    "TrailRun": "carrera por montaña",
    "VirtualRun": "carrera en cinta",
    "Ride": "bici",
    "MountainBikeRide": "bici de montaña",
    "GravelRide": "bici gravel",
    "EBikeRide": "bici eléctrica",
    "VirtualRide": "bici estática",
    "Walk": "caminata",
    "Hike": "senderismo",
    "Swim": "natación",
    "WeightTraining": "pesas",
    "Workout": "entrenamiento",
    "Yoga": "yoga",
    "Rowing": "remo",
    "Elliptical": "elíptica",
    "Crossfit": "crossfit",
}
_RUN_LIKE = {"Run", "TrailRun", "VirtualRun", "Walk", "Hike"}
_DAYS = "lun mar mié jue vie sáb dom".split()


def _api(creds: Any, path: str, **params: Any) -> Any:
    return request(
        "GET",
        f"{API}{path}",
        headers={"Authorization": f"Bearer {creds.token()}"},
        params=params or None,
        auth_error=AUTH_ERROR,
    )


def _duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}min" if h else f"{m}min {s:02d}s"


def _pace(meters: float, seconds: float, sport: str) -> str:
    if not meters or not seconds:
        return ""
    if sport in _RUN_LIKE:
        per_km = seconds / (meters / 1000)
        m, s = divmod(int(round(per_km)), 60)
        return f"{m}:{s:02d} min/km"
    if sport == "Swim":
        per_100 = seconds / (meters / 100)
        m, s = divmod(int(round(per_100)), 60)
        return f"{m}:{s:02d} min/100m"
    return f"{meters / seconds * 3.6:.1f} km/h"


def _sport(a: Dict[str, Any]) -> str:
    return a.get("sport_type") or a.get("type") or ""


def _activity(a: Dict[str, Any]) -> Dict[str, Any]:
    sport = _sport(a)
    when = ""
    if a.get("start_date_local"):
        d = datetime.fromisoformat(a["start_date_local"].replace("Z", ""))
        when = f"{_DAYS[d.weekday()]} {d:%d/%m/%Y %H:%M}"
    dist = a.get("distance") or 0
    return pick(
        {
            "fecha": when,
            "nombre": a.get("name", ""),
            "tipo": _TYPES.get(sport, sport),
            "distancia": f"{dist / 1000:.2f} km" if dist else "",
            "tiempo": _duration(a.get("moving_time") or 0),
            "ritmo": _pace(dist, a.get("moving_time") or 0, sport),
            "desnivel": f"{a['total_elevation_gain']:.0f} m"
            if a.get("total_elevation_gain")
            else "",
            "pulso_medio": f"{a['average_heartrate']:.0f} ppm"
            if a.get("average_heartrate")
            else "",
        },
        "fecha",
        "nombre",
        "tipo",
        "distancia",
        "tiempo",
        "ritmo",
        "desnivel",
        "pulso_medio",
    )


def _summary(acts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Totals by sport for a list of activities."""
    by: Dict[str, Dict[str, float]] = {}
    for a in acts:
        t = by.setdefault(
            _TYPES.get(_sport(a), _sport(a)), {"n": 0, "m": 0, "s": 0, "e": 0}
        )
        t["n"] += 1
        t["m"] += a.get("distance") or 0
        t["s"] += a.get("moving_time") or 0
        t["e"] += a.get("total_elevation_gain") or 0
    return {sport: _total(t["n"], t["m"], t["s"], t["e"]) for sport, t in by.items()}


def _total(count: float, meters: float, seconds: float, elev: float) -> str:
    text = f"{int(count)} actividades, {meters / 1000:.1f} km, {_duration(seconds)}"
    if elev:
        text += f", {elev:.0f} m de desnivel"
    return text


def _athlete_id(creds: Any) -> Any:
    raw = creds.get("athlete")
    if raw:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if data.get("id"):
                return data["id"]
        except (ValueError, AttributeError):
            pass
    return _api(creds, "/athlete")["id"]


def _check(creds: Any) -> str:
    me = _api(creds, "/athlete") or {}
    name = f"{me.get('firstname', '')} {me.get('lastname', '')}".strip()
    return name or me.get("username") or "Strava"


def _tools(creds: Any):
    @tool(
        "strava_recent_activities",
        "Lista las últimas actividades deportivas del usuario en Strava (tipo, "
        "distancia, tiempo, ritmo, desnivel y fecha).",
        {
            "cantidad": I("Cuántas actividades (1-30, por defecto 10)"),
            "dias": I("Solo las de los últimos N días (opcional)"),
        },
        integration="strava",
    )
    def strava_recent_activities(cantidad: int = 10, dias: int = 0) -> Any:
        params: Dict[str, Any] = {"per_page": max(1, min(int(cantidad or 10), 30))}
        if dias:
            params["after"] = int(
                (datetime.now() - timedelta(days=int(dias))).timestamp()
            )
        acts = _api(creds, "/athlete/activities", **params) or []
        if not acts:
            return "No hay actividades en Strava en ese periodo."
        return [_activity(a) for a in acts]

    @tool(
        "strava_stats",
        "Resumen de los totales del usuario en Strava: esta semana, este mes, "
        "este año y desde siempre (carrera, bici y natación).",
        integration="strava",
    )
    def strava_stats() -> Any:
        from openjarvis.personal.reminders import local_tz

        now = datetime.now(local_tz())
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = today - timedelta(days=today.weekday())
        month = today.replace(day=1)
        since = min(week, month)
        acts = (
            _api(
                creds, "/athlete/activities", after=int(since.timestamp()), per_page=200
            )
            or []
        )

        def started(a: Dict[str, Any]) -> datetime:
            return datetime.fromisoformat(a["start_date"].replace("Z", "+00:00"))

        out: Dict[str, Any] = {
            "esta_semana": _summary([a for a in acts if started(a) >= week])
            or "sin actividades",
            "este_mes": _summary([a for a in acts if started(a) >= month])
            or "sin actividades",
        }
        stats = _api(creds, f"/athletes/{_athlete_id(creds)}/stats") or {}
        labels = {"run": "carrera", "ride": "bici", "swim": "natación"}
        for period, key in (("este_año", "ytd"), ("desde_siempre", "all")):
            block = {}
            for sport, label in labels.items():
                t = stats.get(f"{key}_{sport}_totals") or {}
                if t.get("count"):
                    block[label] = _total(
                        t["count"],
                        t.get("distance", 0),
                        t.get("moving_time", 0),
                        t.get("elevation_gain", 0),
                    )
            out[period] = block or "sin actividades de carrera, bici o natación"
        return out

    return [strava_recent_activities, strava_stats]


register(
    Integration(
        id="strava",
        name="Strava",
        category="personal",
        description="Tus entrenamientos y tus kilómetros de la semana, del mes y del año.",
        abilities=[
            "¿Cuánto he corrido esta semana?",
            "¿Qué tal fue mi última salida en bici?",
            "Resumen de mis kilómetros este año",
        ],
        steps=[
            "Pulsa el botón «<b>Conectar con Strava</b>».",
            "Entra con tu cuenta de Strava si te lo pide.",
            "Deja marcada la casilla de ver tus actividades (también las privadas) y pulsa «<b>Autorizar</b>».",
            "¡Listo! Jarvis solo puede ver tus datos, nunca publica ni cambia nada.",
        ],
        oauth=OAuthSpec(
            provider="strava",
            authorize_url="https://www.strava.com/oauth/authorize",
            token_url="https://www.strava.com/oauth/token",
            scopes=["read", "activity:read_all"],
            client_env="STRAVA",
            extra_params={"approval_prompt": "auto"},
            scope_separator=",",
        ),
        check=_check,
        build_tools=_tools,
        prompt="Herramientas strava_*: últimas actividades (strava_recent_activities) y totales (strava_stats). Solo lectura.",
        icon="🏃",
        color="#FC4C02",
        link="https://www.strava.com",
    )
)
