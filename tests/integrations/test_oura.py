"""Tests for the Oura integration (OAuth)."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry
from openjarvis.integrations.manager import Creds
from openjarvis.integrations.providers import oura

TODAY = date(2030, 3, 6)  # a Wednesday
CREDS = Creds({"access_token": "oura-at"}, lambda: "oura-at")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(oura, "_today", lambda: TODAY)
    holder = SimpleNamespace(handler=lambda r: httpx.Response(404), calls=[])

    def dispatch(request):
        holder.calls.append(request)
        return holder.handler(request)

    base.set_http(httpx.Client(transport=httpx.MockTransport(dispatch)))
    yield holder
    base.set_http(None)


def tools_of(creds=CREDS):
    return {t.spec.name: t for t in registry()["oura"].build_tools(creds)}


def test_registered_as_oauth():
    integ = registry()["oura"]
    assert integ.category == "personal"
    assert integ.oauth is not None and integ.oauth.client_env == "OURA"
    assert integ.oauth.authorize_url == "https://cloud.ouraring.com/oauth/authorize"
    assert integ.oauth.token_url == "https://api.ouraring.com/oauth/token"
    assert "daily" in integ.oauth.scopes
    assert integ.fields == []
    assert set(tools_of()) == {"oura_sleep", "oura_readiness", "oura_activity"}


def test_check_and_expired(api):
    api.handler = lambda r: httpx.Response(
        200, json={"id": "u1", "email": "ana@ejemplo.es", "age": 40}
    )
    assert registry()["oura"].check(CREDS) == "ana@ejemplo.es"
    req = api.calls[0]
    assert str(req.url) == "https://api.ouraring.com/v2/usercollection/personal_info"
    assert req.headers["authorization"] == "Bearer oura-at"
    # Plain dicts (no refresh callback) also work.
    registry()["oura"].check({"access_token": "plain"})
    assert api.calls[-1].headers["authorization"] == "Bearer plain"

    api.handler = lambda r: httpx.Response(401, json={"detail": "expired"})
    with pytest.raises(IntegrationError, match="Vuelve a conectarlo"):
        registry()["oura"].check(CREDS)
    assert not tools_of()["oura_sleep"].execute().success


def test_sleep(api):
    def handler(req):
        if req.url.path.endswith("/daily_sleep"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"day": "2030-03-05", "score": 82},
                        {"day": "2030-03-06", "score": 74},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "day": "2030-03-06",
                        "type": "long_sleep",
                        "total_sleep_duration": 26700,
                        "efficiency": 88,
                        "deep_sleep_duration": 4500,
                        "rem_sleep_duration": 5400,
                        "lowest_heart_rate": 52,
                        "bedtime_start": "2030-03-05T23:40:00+01:00",
                        "bedtime_end": "2030-03-06T07:35:00+01:00",
                    },
                    {
                        "day": "2030-03-06",
                        "type": "late_nap",
                        "total_sleep_duration": 1200,
                    },
                    {"day": "2030-03-07", "total_sleep_duration": 100},  # out of range
                ]
            },
        )

    api.handler = handler
    out = tools_of()["oura_sleep"].execute(dias=2).content
    lines = out.splitlines()
    assert lines[1] == (
        "- mié 06/03: puntuación 74, dormido 7 h 25 min, eficiencia 88%, "
        "profundo 1 h 15 min, REM 1 h 30 min, pulso mín. 52 ppm, de 23:40 a 07:35"
    )
    assert lines[2] == "- mar 05/03: puntuación 82"
    assert len(lines) == 3
    for req in api.calls:
        assert req.url.params["start_date"] == "2030-03-05"
        assert req.url.params["end_date"] == "2030-03-07"
    assert {r.url.path.rsplit("/", 1)[-1] for r in api.calls} == {
        "daily_sleep",
        "sleep",
    }

    api.handler = lambda r: httpx.Response(200, json={"data": []})
    assert "no tiene datos" in tools_of()["oura_sleep"].execute().content


def test_readiness(api):
    api.handler = lambda r: httpx.Response(
        200,
        json={
            "data": [{"day": "2030-03-06", "score": 86, "temperature_deviation": -0.21}]
        },
    )
    out = tools_of()["oura_readiness"].execute(dias=1).content
    assert (
        "- mié 06/03: puntuación 86, temperatura -0,2 °C respecto a lo habitual" in out
    )
    req = api.calls[0]
    assert req.url.path == "/v2/usercollection/daily_readiness"
    assert req.url.params["start_date"] == TODAY.isoformat()


def test_activity(api):
    api.handler = lambda r: httpx.Response(
        200,
        json={
            "data": [
                {
                    "day": "2030-03-05",
                    "score": 90,
                    "steps": 12345,
                    "active_calories": 520,
                    "total_calories": 2600,
                    "equivalent_walking_distance": 9800,
                }
            ]
        },
    )
    out = tools_of()["oura_activity"].execute(dias=40).content
    assert (
        "- mar 05/03: 12.345 pasos, 520 kcal activas, 2600 kcal en total, "
        "9,8 km, puntuación 90" in out
    )
    start = date.fromisoformat(api.calls[0].url.params["start_date"])
    assert TODAY - start == timedelta(days=30)  # capped at 31 days
