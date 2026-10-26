# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Weather forecast from Open-Meteo: free, no key, always available."""

from __future__ import annotations

from typing import Any, Dict

from openjarvis.integrations.base import (
    I,
    Integration,
    IntegrationError,
    S,
    register,
    request,
    tool,
)

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"

_CODES = {
    0: "despejado",
    1: "casi despejado",
    2: "parcialmente nuboso",
    3: "nublado",
    45: "niebla",
    48: "niebla con escarcha",
    51: "llovizna débil",
    53: "llovizna",
    55: "llovizna intensa",
    61: "lluvia débil",
    63: "lluvia",
    65: "lluvia fuerte",
    66: "lluvia helada",
    67: "lluvia helada fuerte",
    71: "nieve débil",
    73: "nieve",
    75: "nieve fuerte",
    77: "granizo fino",
    80: "chubascos",
    81: "chubascos fuertes",
    82: "chubascos violentos",
    85: "chubascos de nieve",
    86: "nevadas fuertes",
    95: "tormenta",
    96: "tormenta con granizo",
    99: "tormenta fuerte con granizo",
}


def _place(name: str) -> Dict[str, Any]:
    data = request("GET", GEO, params={"name": name, "count": 1, "language": "es"})
    results = (data or {}).get("results") or []
    if not results:
        raise IntegrationError(f"No encuentro la localidad «{name}».")
    return results[0]


def _tools(creds: Dict[str, str]):
    @tool(
        "weather_forecast",
        "Previsión del tiempo para una localidad: hoy y los próximos días "
        "(temperaturas, lluvia, viento).",
        {
            "lugar": S("Ciudad o pueblo, p. ej. «Valencia» o «Getafe, Madrid»"),
            "dias": I("Días de previsión (1-14, por defecto 3)"),
        },
        ["lugar"],
        integration="weather",
    )
    def weather_forecast(lugar: str, dias: int = 3) -> str:
        place = _place(lugar)
        data = request(
            "GET",
            FORECAST,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "timezone": "auto",
                "forecast_days": max(1, min(int(dias or 3), 14)),
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
            },
        )
        where = ", ".join(
            x
            for x in (place.get("name"), place.get("admin1"), place.get("country"))
            if x
        )
        lines = [f"Tiempo en {where}:"]
        cur = data.get("current") or {}
        if cur:
            lines.append(
                f"Ahora: {cur.get('temperature_2m')} °C, "
                f"{_CODES.get(cur.get('weather_code'), 'variable')}, "
                f"viento {cur.get('wind_speed_10m')} km/h"
            )
        d = data.get("daily") or {}
        for i, day in enumerate(d.get("time", [])):
            lines.append(
                f"{day}: {_CODES.get(d['weather_code'][i], 'variable')}, "
                f"{d['temperature_2m_min'][i]}–{d['temperature_2m_max'][i]} °C, "
                f"lluvia {d['precipitation_probability_max'][i]}% "
                f"({d['precipitation_sum'][i]} mm), viento máx "
                f"{d['wind_speed_10m_max'][i]} km/h"
            )
        return "\n".join(lines)

    return [weather_forecast]


register(
    Integration(
        id="weather",
        name="El tiempo",
        category="personal",
        description="Previsión meteorológica de cualquier lugar. Siempre activo.",
        abilities=[
            "¿Qué tiempo hará el sábado en Valencia?",
            "¿Llevo paraguas mañana?",
        ],
        steps=["No hay que configurar nada: ya está activo."],
        build_tools=_tools,
        prompt="weather_forecast da la previsión de cualquier localidad.",
        always_on=True,
        icon="🌤️",
        color="#00d9ff",
        link="https://open-meteo.com",
    )
)
