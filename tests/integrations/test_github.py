"""Tests for the GitHub integration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry

CREDS = {"token": "ghp_abc"}


def issue(n, title, *, pr=False, repo="acme/web", updated="2030-03-04T09:00:00Z"):
    it = {
        "number": n,
        "title": title,
        "state": "open",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "html_url": f"https://github.com/{repo}/issues/{n}",
        "updated_at": updated,
        "comments": 0,
    }
    if pr:
        it["pull_request"] = {"url": "x"}
    return it


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


def tools(creds=CREDS):
    return {t.spec.name: t for t in registry()["github"].build_tools(creds)}


def test_check(api):
    api.handler = lambda r: httpx.Response(200, json={"login": "octocat"})
    assert registry()["github"].check(CREDS) == "octocat"
    req = api.calls[0]
    assert req.url.host == "api.github.com" and req.url.path == "/user"
    assert req.headers["authorization"] == "Bearer ghp_abc"
    assert req.headers["x-github-api-version"] == "2022-11-28"


def test_bad_token(api):
    api.handler = lambda r: httpx.Response(401, json={"message": "Bad credentials"})
    with pytest.raises(IntegrationError, match="token"):
        registry()["github"].check(CREDS)


def test_notifications(api):
    api.handler = lambda r: httpx.Response(
        200,
        json=[
            {
                "repository": {"full_name": "acme/web"},
                "subject": {"title": "Arreglar login", "type": "PullRequest"},
                "reason": "review_requested",
                "updated_at": "2030-03-04T09:00:00Z",
            }
        ],
    )
    data = json.loads(tools()["github_notifications"].execute().content)
    assert data == [
        {
            "repo": "acme/web",
            "tipo": "PR",
            "titulo": "Arreglar login",
            "motivo": "te piden revisión",
            "fecha": "04/03/2030 10:00",
        }
    ]
    assert api.calls[0].url.path == "/notifications"
    api.handler = lambda r: httpx.Response(200, json=[])
    assert (
        "No tienes notificaciones" in tools()["github_notifications"].execute().content
    )
    out = tools({"token": "github_pat_x"})["github_notifications"].execute()
    assert not out.success and "clásico" in out.content


def test_search_splits_issues_and_prs(api):
    def handler(r):
        q = r.url.params["q"]
        if q.endswith("is:issue"):
            return httpx.Response(200, json={"items": [issue(1, "Bug viejo")]})
        return httpx.Response(
            200,
            json={
                "items": [issue(2, "PR nuevo", pr=True, updated="2030-03-05T09:00:00Z")]
            },
        )

    api.handler = handler
    data = json.loads(tools()["github_search"].execute(consulta="login").content)
    assert [d["numero"] for d in data] == [2, 1]
    assert data[0]["tipo"] == "PR" and data[1]["tipo"] == "issue"
    assert data[0]["repo"] == "acme/web" and data[0]["estado"] == "abierto"
    assert [c.url.params["q"] for c in api.calls] == ["login is:issue", "login is:pr"]
    api.calls.clear()
    tools()["github_search"].execute(consulta="repo:acme/web is:pr bug")
    assert len(api.calls) == 1


def test_my_items(api):
    def handler(r):
        if r.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        q = r.url.params["q"]
        if "author:octocat" in q and "is:pr" in q:
            return httpx.Response(200, json={"items": [issue(5, "Mi PR", pr=True)]})
        if "assignee:octocat" in q:
            return httpx.Response(200, json={"items": [issue(7, "Asignado")]})
        if "author:octocat" in q:  # duplicate of the assigned one
            return httpx.Response(200, json={"items": [issue(7, "Asignado")]})
        return httpx.Response(200, json={"items": []})

    api.handler = handler
    data = json.loads(tools()["github_my_items"].execute().content)
    assert list(data) == ["mis PRs", "issues asignados a mí"]
    assert data["mis PRs"][0]["numero"] == 5
    assert data["issues asignados a mí"][0]["titulo"] == "Asignado"


def test_read_issue_with_comments(api):
    def handler(r):
        if r.url.path == "/repos/acme/web/issues/42":
            it = issue(42, "El formulario no envía")
            it.update(
                {
                    "comments": 12,
                    "body": "Al pulsar enviar no pasa nada.",
                    "user": {"login": "ana"},
                    "created_at": "2030-03-01T08:00:00Z",
                    "labels": [{"name": "bug"}],
                    "assignees": [{"login": "octocat"}],
                }
            )
            return httpx.Response(200, json=it)
        assert r.url.path == "/repos/acme/web/issues/42/comments"
        assert r.url.params["page"] == "2" and r.url.params["per_page"] == "10"
        return httpx.Response(
            200,
            json=[
                {
                    "user": {"login": "pepe"},
                    "body": "Confirmado",
                    "created_at": "2030-03-02T08:00:00Z",
                },
                {
                    "user": {"login": "ana"},
                    "body": "Gracias",
                    "created_at": "2030-03-03T08:00:00Z",
                },
            ],
        )

    api.handler = handler
    out = tools()["github_read_issue"].execute(
        repo="https://github.com/acme/web", numero=42
    )
    assert out.success, out.content
    text = out.content
    assert text.startswith("Issue #42: El formulario no envía (abierto)")
    assert "Autor: ana · 01/03/2030 09:00" in text
    assert "Etiquetas: bug" in text and "Asignado a: octocat" in text
    assert "Últimos comentarios (2 de 12):" in text
    assert "— ana (03/03/2030 09:00): Gracias" in text
    bad = tools()["github_read_issue"].execute(repo="web", numero=1)
    assert not bad.success and "usuario/repositorio" in bad.content


def test_create_issue_and_comment(api):
    def handler(r):
        if r.url.path.endswith("/comments"):
            return httpx.Response(
                201, json={"html_url": "https://github.com/acme/web/issues/42#c1"}
            )
        return httpx.Response(
            201,
            json={"number": 43, "html_url": "https://github.com/acme/web/issues/43"},
        )

    api.handler = handler
    t = tools()
    assert base.CONFIRM_NOTE in t["github_create_issue"].spec.description
    out = t["github_create_issue"].execute(
        repo="acme/web", titulo="Nuevo", descripcion="Detalle"
    )
    assert out.content == "Issue #43 creado: https://github.com/acme/web/issues/43"
    req = api.calls[-1]
    assert req.method == "POST" and req.url.path == "/repos/acme/web/issues"
    assert json.loads(req.content) == {"title": "Nuevo", "body": "Detalle"}

    out = t["github_comment"].execute(repo="acme/web", numero=42, texto="Hecho")
    assert (
        out.content == "Comentario publicado: https://github.com/acme/web/issues/42#c1"
    )
    req = api.calls[-1]
    assert req.url.path == "/repos/acme/web/issues/42/comments"
    assert json.loads(req.content) == {"body": "Hecho"}
