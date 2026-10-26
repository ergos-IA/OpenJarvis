"""Tests for the calendar (iCal), weather, Notion and MCP integrations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry
from openjarvis.integrations.providers import agenda

MAD = ZoneInfo("Europe/Madrid")


@pytest.fixture
def api():
    holder = SimpleNamespace(handler=lambda r: httpx.Response(404), calls=[])

    def dispatch(request):
        holder.calls.append(request)
        return holder.handler(request)

    base.set_http(httpx.Client(transport=httpx.MockTransport(dispatch)))
    agenda._CACHE.clear()
    yield holder
    base.set_http(None)


def tools_of(integration_id, creds):
    return {t.spec.name: t for t in registry()[integration_id].build_tools(creds)}


ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:weekly-1\r
DTSTART;TZID=Romance Standard Time:20300304T100000\r
DTEND;TZID=Romance Standard Time:20300304T110000\r
RRULE:FREQ=WEEKLY;BYDAY=MO\r
EXDATE;TZID=Romance Standard Time:20300311T100000\r
SUMMARY:Comité semanal\r
LOCATION:Sala 2\\, planta 1\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:weekly-1\r
RECURRENCE-ID;TZID=Romance Standard Time:20300318T100000\r
DTSTART;TZID=Romance Standard Time:20300318T120000\r
DTEND;TZID=Romance Standard Time:20300318T130000\r
SUMMARY:Comité semanal (movido)\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:once\r
DTSTART:20300305T080000Z\r
DURATION:PT30M\r
SUMMARY:Llamada con Ana López\r
DESCRIPTION:Hablar del presupuesto\r
 para 2031\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:holiday\r
DTSTART;VALUE=DATE:20300319\r
SUMMARY:San José\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:gone\r
DTSTART:20300306T080000Z\r
SUMMARY:Cancelado\r
STATUS:CANCELLED\r
END:VEVENT\r
END:VCALENDAR\r
"""


class TestAgenda:
    def test_parse_and_expand(self):
        events = agenda.parse_ics(ICS)
        assert len(events) == 4  # cancelled one dropped
        once = next(e for e in events if e["uid"] == "once")
        assert once["end"] - once["start"] == timedelta(minutes=30)
        # RFC 5545 unfolding drops the CRLF and the one leading space.
        assert once["description"] == "Hablar del presupuestopara 2031"
        start = datetime(2030, 3, 1, tzinfo=MAD)
        occ = agenda.expand(events, start, start + timedelta(days=21))
        titles = [
            (e["occurrence"].astimezone(MAD).strftime("%d %H:%M"), e["summary"])
            for e in occ
        ]
        assert ("04 10:00", "Comité semanal") in titles
        assert not any(t[0].startswith("11") for t in titles)  # EXDATE
        assert ("18 12:00", "Comité semanal (movido)") in titles  # override
        assert not any(t == ("18 10:00", "Comité semanal") for t in titles)
        assert ("19 00:00", "San José") in titles

    def test_not_a_calendar(self):
        with pytest.raises(IntegrationError, match="calendario"):
            agenda.parse_ics("<html>login</html>")

    def test_tool_and_check(self, api, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Madrid")
        api.handler = lambda r: httpx.Response(200, text=ICS)
        creds = {"urls": "webcal://calendar.example.com/basic.ics"}
        monkeypatch.setattr("openjarvis.security.ssrf.check_ssrf", lambda url: None)
        assert "eventos" in registry()["agenda"].check(creds)
        assert str(api.calls[0].url).startswith("https://calendar.example.com")
        t = tools_of("agenda", creds)["agenda_events"]
        out = t.execute(desde="2030-03-04", dias=2).content
        assert "lun 04/03 10:00-11:00 · Comité semanal · Sala 2, planta 1" in out
        assert "mar 05/03 09:00-09:30 · Llamada con Ana López" in out
        assert "Ana" in t.execute(desde="2030-03-01", dias=30, buscar="ana").content
        assert "No hay eventos" in t.execute(desde="2031-01-01", dias=1).content


class TestWeather:
    def test_forecast(self, api):
        def handler(request):
            if "geocoding" in request.url.host:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "name": "Valencia",
                                "admin1": "Comunidad Valenciana",
                                "country": "España",
                                "latitude": 39.47,
                                "longitude": -0.38,
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "current": {
                        "temperature_2m": 21.5,
                        "weather_code": 1,
                        "wind_speed_10m": 9,
                    },
                    "daily": {
                        "time": ["2030-03-09"],
                        "weather_code": [61],
                        "temperature_2m_max": [19],
                        "temperature_2m_min": [11],
                        "precipitation_probability_max": [70],
                        "precipitation_sum": [3.2],
                        "wind_speed_10m_max": [20],
                    },
                },
            )

        api.handler = handler
        out = (
            tools_of("weather", {})["weather_forecast"]
            .execute(lugar="Valencia")
            .content
        )
        assert (
            "Valencia, Comunidad Valenciana" in out
            and "lluvia débil" in out
            and "70%" in out
        )

    def test_unknown_place(self, api):
        api.handler = lambda r: httpx.Response(200, json={})
        res = tools_of("weather", {})["weather_forecast"].execute(lugar="Xyzzy")
        assert not res.success and "No encuentro" in res.content


class TestNotion:
    def test_check_search_read_write(self, api):
        def handler(request):
            assert request.headers["Authorization"] == "Bearer ntn_x"
            path = request.url.path
            if path == "/v1/users/me":
                return httpx.Response(200, json={"bot": {"workspace_name": "Acme"}})
            if path == "/v1/search":
                assert json.loads(request.content)["query"] == "acta"
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "object": "page",
                                "id": "p1",
                                "url": "https://notion.so/p1",
                                "last_edited_time": "2030-01-02T00:00:00Z",
                                "properties": {
                                    "Name": {
                                        "type": "title",
                                        "title": [{"plain_text": "Acta lunes"}],
                                    }
                                },
                            }
                        ]
                    },
                )
            if path == "/v1/pages/p1":
                return httpx.Response(
                    200,
                    json={
                        "id": "p1",
                        "properties": {
                            "Name": {
                                "type": "title",
                                "title": [{"plain_text": "Acta lunes"}],
                            },
                            "Estado": {"type": "select", "select": {"name": "Hecho"}},
                        },
                    },
                )
            if path == "/v1/blocks/p1/children" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "type": "heading_2",
                                "heading_2": {"rich_text": [{"plain_text": "Puntos"}]},
                            },
                            {
                                "type": "to_do",
                                "to_do": {
                                    "checked": True,
                                    "rich_text": [{"plain_text": "Presupuesto"}],
                                },
                            },
                        ]
                    },
                )
            if path == "/v1/blocks/p1/children" and request.method == "PATCH":
                body = json.loads(request.content)["children"]
                assert [b["type"] for b in body] == [
                    "paragraph",
                    "bulleted_list_item",
                    "to_do",
                ]
                return httpx.Response(200, json={})
            return httpx.Response(404)

        api.handler = handler
        creds = {"token": "ntn_x"}
        assert registry()["notion"].check(creds) == "Acme"
        t = tools_of("notion", creds)
        assert "Acta lunes" in t["notion_search"].execute(texto="acta").content
        page = t["notion_read"].execute(id="p1").content
        assert (
            "# Acta lunes" in page
            and "Estado: Hecho" in page
            and "[x] Presupuesto" in page
        )
        assert (
            "3 bloques"
            in t["notion_add_to_page"]
            .execute(id="p1", texto="Hola\n- uno\n[ ] dos")
            .content
        )
        assert t["notion_add_to_page"].spec.metadata["writes"]

    def test_bad_token(self, api):
        api.handler = lambda r: httpx.Response(401, json={})
        with pytest.raises(IntegrationError, match="clave"):
            registry()["notion"].check({"token": "nope"})


class TestMCP:
    def test_rejects_http_and_private(self):
        with pytest.raises(IntegrationError, match="https"):
            registry()["mcp"].check({"url": "http://example.com/mcp"})
        with pytest.raises(IntegrationError, match="no permitida"):
            registry()["mcp"].check({"url": "https://127.0.0.1/mcp"})
