"""Tests for the Google OAuth integration (Calendar, Drive, Contacts, Tasks)."""

from __future__ import annotations

import json
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


CREDS = Creds({}, lambda: "tok")


def tools():
    return {t.spec.name: t for t in registry()["google"].build_tools(CREDS)}


def q(request):
    return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


def test_oauth_spec(tmp_path, monkeypatch):
    integ = registry()["google"]
    spec = integ.oauth
    assert spec.client_env == "GOOGLE"
    assert spec.token_url == "https://oauth2.googleapis.com/token"
    assert spec.extra_params == {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    joined = " ".join(spec.scopes)
    for s in ("calendar.events", "drive.readonly", "contacts.readonly", "auth/tasks"):
        assert s in joined
    assert "openid" in spec.scopes and "email" in spec.scopes
    assert "mail" not in joined.replace("email", "")  # no Gmail scopes
    assert integ.category == "productividad" and integ.check and integ.prompt
    monkeypatch.setenv("OPENJARVIS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("OPENJARVIS_GOOGLE_CLIENT_SECRET", "sec")
    url = IntegrationManager(tmp_path / "i.json").begin_oauth("google", "h.test")
    params = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert params["access_type"] == ["offline"] and params["client_id"] == ["cid"]
    assert "https://www.googleapis.com/auth/tasks" in params["scope"][0].split(" ")


def test_check_and_401(api):
    def handler(r):
        assert r.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"email": "ana@gmail.com"})

    api.handler = handler
    assert registry()["google"].check(CREDS) == "ana@gmail.com"
    api.handler = lambda r: httpx.Response(401, json={})
    with pytest.raises(IntegrationError, match="Vuelve a conectar Google"):
        registry()["google"].check(CREDS)
    res = tools()["google_drive_search"].execute(texto="x")
    assert not res.success and "Google" in res.content


def test_calendar_events(api):
    def handler(r):
        path = r.url.path
        if path.endswith("/calendarList"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "ana@gmail.com", "primary": True},
                        {"id": "fam@group.calendar.google.com", "summary": "Familia"},
                        {"id": "hidden@x", "hidden": True},
                    ]
                },
            )
        params = q(r)
        assert params["singleEvents"] == "true" and params["q"] == "dentista"
        assert params["timeMin"] == "2030-03-04T00:00:00+01:00"
        assert params["timeMax"] == "2030-03-06T00:00:00+01:00"
        if "/calendars/primary/" in path:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "e2",
                            "summary": "Dentista",
                            "location": "C/ Mayor 1",
                            "start": {"dateTime": "2030-03-05T16:00:00Z"},
                            "end": {"dateTime": "2030-03-05T17:00:00Z"},
                        },
                        {
                            "id": "x",
                            "status": "cancelled",
                            "start": {"date": "2030-03-05"},
                        },
                    ]
                },
            )
        assert "fam%40group" in str(r.url) or "fam@group" in path
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "e1",
                        "summary": "Cumple dentista",
                        "start": {"date": "2030-03-04"},
                        "end": {"date": "2030-03-05"},
                    }
                ]
            },
        )

    api.handler = handler
    out = json.loads(
        tools()["google_calendar_events"]
        .execute(desde="2030-03-04", dias=2, buscar="dentista")
        .content
    )
    assert out[0] == {
        "id": "fam@group.calendar.google.com::e1",
        "cuando": "lun 04/03 (todo el día)",
        "titulo": "Cumple dentista",
        "calendario": "Familia",
    }
    assert out[1]["cuando"] == "mar 05/03 17:00-18:00"
    assert out[1]["id"] == "e2" and out[1]["lugar"] == "C/ Mayor 1"
    assert len(out) == 2
    assert not any("hidden" in str(c.url) for c in api.calls)


def test_create_update_delete_event(api):
    seen = []

    def handler(r):
        seen.append(r)
        if r.method == "POST":
            body = json.loads(r.content)
            assert q(r)["sendUpdates"] == "none"
            assert body["summary"] == "Dentista"
            assert body["start"] == {
                "dateTime": "2030-03-07T17:00:00+01:00",
                "timeZone": "Europe/Madrid",
            }
            assert body["end"]["dateTime"] == "2030-03-07T17:30:00+01:00"
            assert body["attendees"] == [{"email": "eva@x.com"}]
            return httpx.Response(200, json={"id": "new1", **body})
        if r.method == "GET":
            return httpx.Response(
                200,
                json={
                    "start": {"dateTime": "2030-03-07T17:00:00+01:00"},
                    "end": {"dateTime": "2030-03-07T18:30:00+01:00"},
                },
            )
        if r.method == "PATCH":
            body = json.loads(r.content)
            assert "/calendars/fam@g/events/e1" in r.url.path
            assert body["start"]["dateTime"] == "2030-03-08T10:00:00+01:00"
            assert body["end"]["dateTime"] == "2030-03-08T11:30:00+01:00"  # kept 90'
            return httpx.Response(200, json={"summary": "Cita", **body})
        if r.method == "DELETE":
            assert r.url.path.endswith("/calendars/primary/events/e2")
            assert q(r)["sendUpdates"] == "none"
            return httpx.Response(204)
        return httpx.Response(404)

    api.handler = handler
    t = tools()
    assert t["google_calendar_create_event"].spec.metadata["writes"]
    out = t["google_calendar_create_event"].execute(
        titulo="Dentista",
        inicio="2030-03-07 17:00",
        duracion_min=30,
        invitados="eva@x.com",
    )
    assert out.success and "jue 07/03 17:00-17:30 · Dentista" in out.content
    out = t["google_calendar_update_event"].execute(
        id="fam@g::e1", inicio="2030-03-08 10:00"
    )
    assert out.success and "vie 08/03 10:00-11:30" in out.content, out.content
    assert (
        t["google_calendar_delete_event"].execute(id="e2").content == "Evento borrado."
    )
    bad = t["google_calendar_create_event"].execute(titulo="x", inicio="mañana")
    assert not bad.success and "AAAA-MM-DD" in bad.content


def test_all_day_event_end_is_exclusive(api):
    def handler(r):
        body = json.loads(r.content)
        assert body["start"] == {"date": "2030-08-01"}
        assert body["end"] == {"date": "2030-08-16"}
        return httpx.Response(200, json={"id": "v", **body, "summary": "Vacaciones"})

    api.handler = handler
    out = tools()["google_calendar_create_event"].execute(
        titulo="Vacaciones", inicio="2030-08-01", fin="2030-08-15"
    )
    assert "todo el día" in out.content


def test_drive_search_and_read(api):
    def handler(r):
        path = r.url.path
        if path == "/drive/v3/files":
            params = q(r)
            assert params["q"] == (
                "trashed = false and (name contains 'presu\\'s' or "
                "fullText contains 'presu\\'s')"
            )
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "d1",
                            "name": "Presupuesto",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2030-01-02T10:00:00Z",
                            "webViewLink": "https://docs.google.com/d1",
                        }
                    ]
                },
            )
        if path == "/drive/v3/files/d1":
            return httpx.Response(
                200,
                json={
                    "name": "Presupuesto",
                    "mimeType": "application/vnd.google-apps.document",
                },
            )
        if path == "/drive/v3/files/d1/export":
            assert q(r)["mimeType"] == "text/plain"
            return httpx.Response(200, text="Total: 1.200 €" + "x" * 6000)
        if path == "/drive/v3/files/s1":
            return httpx.Response(
                200,
                json={
                    "name": "Gastos",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                },
            )
        if path == "/drive/v3/files/s1/export":
            assert q(r)["mimeType"] == "text/csv"
            return httpx.Response(
                200, text="a,b\n1,2", headers={"content-type": "text/csv"}
            )
        if path == "/drive/v3/files/p1":
            return httpx.Response(
                200, json={"name": "foto.jpg", "mimeType": "image/jpeg"}
            )
        return httpx.Response(404)

    api.handler = handler
    t = tools()
    found = json.loads(t["google_drive_search"].execute(texto="presu's").content)
    assert found == [
        {
            "id": "d1",
            "nombre": "Presupuesto",
            "tipo": "documento",
            "modificado": "2030-01-02",
            "enlace": "https://docs.google.com/d1",
        }
    ]
    doc = t["google_drive_read"].execute(id="d1").content
    assert doc.startswith("# Presupuesto\nTotal: 1.200 €") and "(recortado)" in doc
    assert "a,b\n1,2" in t["google_drive_read"].execute(id="s1").content
    assert "imagen" in t["google_drive_read"].execute(id="p1").content


def test_contacts_search(api):
    def handler(r):
        params = q(r)
        assert r.url.path == "/v1/people:searchContacts"
        assert "phoneNumbers" in params["readMask"]
        if not params.get("query"):
            return httpx.Response(200, json={})  # warm-up
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "person": {
                            "names": [{"displayName": "Marta Ruiz"}],
                            "phoneNumbers": [{"value": "+34 600 000 000"}],
                            "emailAddresses": [{"value": "marta@x.com"}],
                        }
                    }
                ]
            },
        )

    api.handler = handler
    out = json.loads(tools()["google_contacts_search"].execute(texto="Marta").content)
    assert out == [
        {
            "nombre": "Marta Ruiz",
            "emails": "marta@x.com",
            "telefonos": "+34 600 000 000",
        }
    ]
    assert len(api.calls) == 2  # warm-up + search
    api.handler = lambda r: httpx.Response(200, json={})
    assert (
        "No encuentro" in tools()["google_contacts_search"].execute(texto="Zz").content
    )


def test_tasks(api):
    def handler(r):
        path = r.url.path
        if path == "/tasks/v1/users/@me/lists":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "L1", "title": "Compra"},
                        {"id": "L2", "title": "Mis tareas"},
                    ]
                },
            )
        if path == "/tasks/v1/lists/@default/tasks" and r.method == "GET":
            assert q(r)["showCompleted"] == "false"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "t1",
                            "title": "Llamar al banco",
                            "due": "2030-03-04T00:00:00.000Z",
                        },
                        {"id": "t0", "title": ""},
                    ]
                },
            )
        if path == "/tasks/v1/lists/L1/tasks" and r.method == "POST":
            body = json.loads(r.content)
            assert body == {"title": "Leche", "due": "2030-03-04T00:00:00.000Z"}
            return httpx.Response(200, json={"id": "t9", **body})
        if path == "/tasks/v1/lists/@default/tasks/t1" and r.method == "PATCH":
            assert json.loads(r.content) == {"status": "completed"}
            return httpx.Response(200, json={"title": "Llamar al banco"})
        return httpx.Response(404)

    api.handler = handler
    t = tools()
    listed = json.loads(t["google_tasks_list"].execute().content)
    assert listed == [{"id": "t1", "tarea": "Llamar al banco", "fecha": "2030-03-04"}]
    add = t["google_tasks_add"].execute(
        titulo="Leche", fecha="2030-03-04", lista="compra"
    )
    assert add.content == "Tarea añadida en «Compra»: Leche"
    assert t["google_tasks_add"].spec.metadata["writes"]
    done = t["google_tasks_complete"].execute(id="t1")
    assert done.content == "Tarea completada: Llamar al banco"
    missing = t["google_tasks_add"].execute(titulo="x", lista="Trabajo")
    assert not missing.success and "Compra, Mis tareas" in missing.content
