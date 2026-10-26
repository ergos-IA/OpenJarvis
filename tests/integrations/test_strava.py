"""Tests for the Strava OAuth integration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry
from openjarvis.integrations.manager import Creds, IntegrationManager


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Madrid")
    holder = SimpleNamespace(handler=lambda r: httpx.Response(404), calls=[])

    def dispatch(request):
        holder.calls.append(request)
        return holder.handler(request)

    base.set_http(httpx.Client(transport=httpx.MockTransport(dispatch)))
    yield holder
    base.set_http(None)


def tools(values=None):
    creds = Creds(values or {}, lambda: "tok")
    return {t.spec.name: t for t in registry()["strava"].build_tools(creds)}


RUN = {
    "name": "Rodaje",
    "sport_type": "Run",
    "distance": 10000.0,
    "moving_time": 3300,
    "total_elevation_gain": 85.4,
    "start_date_local": "2030-03-04T07:30:00Z",
    "average_heartrate": 151.2,
}


def test_oauth_spec(tmp_path, monkeypatch):
    spec = registry()["strava"].oauth
    assert spec.client_env == "STRAVA" and spec.scope_separator == ","
    assert "activity:read_all" in spec.scopes
    assert spec.token_url == "https://www.strava.com/oauth/token"
    monkeypatch.setenv("OPENJARVIS_STRAVA_CLIENT_ID", "1")
    monkeypatch.setenv("OPENJARVIS_STRAVA_CLIENT_SECRET", "s")
    url = IntegrationManager(tmp_path / "i.json").begin_oauth("strava", "h")
    params = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.strava.com/oauth/authorize?")
    assert params["scope"] == ["read,activity:read_all"]


def test_check_and_401(api):
    def handler(r):
        assert r.headers["Authorization"] == "Bearer tok"
        assert r.url.path == "/api/v3/athlete"
        return httpx.Response(
            200, json={"id": 7, "firstname": "Ana", "lastname": "Gil"}
        )

    api.handler = handler
    assert registry()["strava"].check(Creds({}, lambda: "tok")) == "Ana Gil"
    api.handler = lambda r: httpx.Response(401, json={})
    with pytest.raises(IntegrationError, match="Strava"):
        registry()["strava"].check(Creds({}, lambda: "tok"))
    res = tools()["strava_recent_activities"].execute()
    assert not res.success and "Vuelve a conectar Strava" in res.content


def test_recent_activities(api):
    def handler(r):
        params = parse_qs(r.url.query.decode())
        assert params["per_page"] == ["5"] and "after" in params
        swim = {"sport_type": "Swim", "distance": 1500, "moving_time": 1800}
        ride = {"sport_type": "Ride", "distance": 40000, "moving_time": 4800}
        return httpx.Response(200, json=[RUN, swim, ride])

    api.handler = handler
    out = json.loads(
        tools()["strava_recent_activities"].execute(cantidad=5, dias=7).content
    )
    assert out[0] == {
        "fecha": "lun 04/03/2030 07:30",
        "nombre": "Rodaje",
        "tipo": "carrera",
        "distancia": "10.00 km",
        "tiempo": "55min 00s",
        "ritmo": "5:30 min/km",
        "desnivel": "85 m",
        "pulso_medio": "151 ppm",
    }
    assert out[1]["ritmo"] == "2:00 min/100m" and out[1]["tipo"] == "natación"
    assert out[2]["ritmo"] == "30.0 km/h" and out[2]["tiempo"] == "1h 20min"
    api.handler = lambda r: httpx.Response(200, json=[])
    assert "No hay actividades" in tools()["strava_recent_activities"].execute().content


def test_stats_uses_saved_athlete(api):
    now = datetime.now(timezone.utc)

    def handler(r):
        if r.url.path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[{**RUN, "start_date": now.strftime("%Y-%m-%dT%H:%M:%SZ")}],
            )
        assert r.url.path == "/api/v3/athletes/42/stats"
        return httpx.Response(
            200,
            json={
                "ytd_run_totals": {
                    "count": 20,
                    "distance": 200000.0,
                    "moving_time": 72000,
                    "elevation_gain": 1500,
                },
                "ytd_ride_totals": {"count": 0},
                "all_run_totals": {
                    "count": 300,
                    "distance": 3000000,
                    "moving_time": 1080000,
                },
            },
        )

    api.handler = handler
    out = json.loads(
        tools({"athlete": json.dumps({"id": 42})})["strava_stats"].execute().content
    )
    assert out["esta_semana"] == {
        "carrera": "1 actividades, 10.0 km, 55min 00s, 85 m de desnivel"
    }
    assert out["este_año"] == {
        "carrera": "20 actividades, 200.0 km, 20h 00min, 1500 m de desnivel"
    }
    assert out["desde_siempre"]["carrera"].startswith("300 actividades, 3000.0 km")
    assert not any(c.url.path == "/api/v3/athlete" for c in api.calls)


def test_stats_fetches_athlete_id(api):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def handler(r):
        if r.url.path == "/api/v3/athlete":
            return httpx.Response(200, json={"id": 9})
        if r.url.path == "/api/v3/athlete/activities":
            return httpx.Response(200, json=[{**RUN, "start_date": old}])
        assert r.url.path == "/api/v3/athletes/9/stats"
        return httpx.Response(200, json={})

    api.handler = handler
    out = json.loads(tools()["strava_stats"].execute().content)
    assert out["esta_semana"] == "sin actividades"
    assert "sin actividades" in out["este_año"]
