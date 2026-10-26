"""Tests for the Todoist integration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry

CREDS = {"token": "tok123"}
PROJECTS = {
    "results": [
        {"id": "p1", "name": "Inbox", "inbox_project": True},
        {"id": "p2", "name": "Trabajo"},
    ],
    "next_cursor": None,
}


@pytest.fixture
def api():
    holder = SimpleNamespace(handler=lambda r: httpx.Response(404), calls=[])

    def dispatch(request):
        holder.calls.append(request)
        return holder.handler(request)

    base.set_http(httpx.Client(transport=httpx.MockTransport(dispatch)))
    yield holder
    base.set_http(None)


def tools():
    return {t.spec.name: t for t in registry()["todoist"].build_tools(CREDS)}


def test_metadata():
    integ = registry()["todoist"]
    assert integ.category == "productividad"
    names = set(tools())
    assert names == {
        "todoist_list_tasks",
        "todoist_add_task",
        "todoist_complete_task",
        "todoist_list_projects",
    }
    assert base.CONFIRM_NOTE in tools()["todoist_add_task"].spec.description
    assert base.CONFIRM_NOTE not in tools()["todoist_list_tasks"].spec.description


def test_check(api):
    api.handler = lambda r: httpx.Response(200, json=PROJECTS)
    assert registry()["todoist"].check(CREDS) == "Todoist · 2 proyectos"
    req = api.calls[0]
    assert req.url.host == "api.todoist.com"
    assert req.url.path == "/api/v1/projects"
    assert req.headers["authorization"] == "Bearer tok123"


def test_bad_token(api):
    api.handler = lambda r: httpx.Response(401, text="Unauthorized")
    with pytest.raises(IntegrationError, match="token"):
        registry()["todoist"].check(CREDS)
    out = tools()["todoist_list_projects"].execute()
    assert not out.success and "token" in out.content


def test_list_tasks_with_filter(api, monkeypatch):
    def handler(r):
        if r.url.path.endswith("/projects"):
            return httpx.Response(200, json=PROJECTS)
        assert r.url.path == "/api/v1/tasks/filter"
        assert r.url.params["query"] == "(today | overdue) & #Trabajo"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "t1",
                        "content": "Llamar al gestor",
                        "priority": 4,
                        "project_id": "p2",
                        "due": {"date": "2030-03-04T10:00:00", "is_recurring": False},
                    },
                    {
                        "id": "t2",
                        "content": "Regar",
                        "priority": 1,
                        "project_id": "p1",
                        "due": {
                            "date": "2030-03-05",
                            "is_recurring": True,
                            "string": "cada día",
                        },
                    },
                ],
                "next_cursor": None,
            },
        )

    api.handler = handler
    out = tools()["todoist_list_tasks"].execute(
        filtro="today | overdue", proyecto="Trabajo"
    )
    assert out.success, out.content
    data = json.loads(out.content)
    assert data[0] == {
        "id": "t1",
        "tarea": "Llamar al gestor",
        "fecha": "04/03/2030 10:00",
        "prioridad": "urgente",
        "proyecto": "Trabajo",
    }
    assert data[1]["fecha"] == "05/03/2030 (se repite: cada día)"
    assert "prioridad" not in data[1]


def test_list_tasks_by_project_and_empty(api):
    def handler(r):
        if r.url.path.endswith("/projects"):
            return httpx.Response(200, json=PROJECTS)
        assert r.url.path == "/api/v1/tasks"
        assert r.url.params["project_id"] == "p2"
        return httpx.Response(200, json={"results": [], "next_cursor": None})

    api.handler = handler
    out = tools()["todoist_list_tasks"].execute(proyecto="trabajo")
    assert "No hay tareas" in out.content
    bad = tools()["todoist_list_tasks"].execute(proyecto="Nada")
    assert not bad.success and "No encuentro el proyecto" in bad.content


def test_add_task(api):
    def handler(r):
        if r.url.path.endswith("/projects"):
            return httpx.Response(200, json=PROJECTS)
        assert r.method == "POST" and r.url.path == "/api/v1/tasks"
        body = json.loads(r.content)
        assert body == {
            "content": "Pagar seguro",
            "due_string": "mañana a las 10",
            "due_lang": "es",
            "priority": 4,
            "project_id": "p2",
        }
        return httpx.Response(
            200,
            json={
                "id": "t9",
                "content": "Pagar seguro",
                "due": {"date": "2030-03-05T10:00:00"},
            },
        )

    api.handler = handler
    out = tools()["todoist_add_task"].execute(
        tarea="Pagar seguro", fecha="mañana a las 10", prioridad=1, proyecto="Trabajo"
    )
    assert out.success, out.content
    assert out.content == "Tarea creada: «Pagar seguro» para el 05/03/2030 10:00."


def test_complete_task(api):
    api.handler = lambda r: httpx.Response(204)
    out = tools()["todoist_complete_task"].execute(id="t1")
    assert out.success and "hecha" in out.content
    req = api.calls[0]
    assert req.method == "POST" and req.url.path == "/api/v1/tasks/t1/close"


def test_list_projects(api):
    api.handler = lambda r: httpx.Response(200, json=PROJECTS)
    data = json.loads(tools()["todoist_list_projects"].execute().content)
    assert data == [
        {"id": "p1", "nombre": "Inbox", "bandeja_de_entrada": True},
        {"id": "p2", "nombre": "Trabajo"},
    ]
