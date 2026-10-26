"""Tests for the Slack integration."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry
from openjarvis.integrations.providers import slack

CREDS = {"token": "xoxp-123"}
CHANNELS = {
    "ok": True,
    "channels": [
        {"id": "C0000001", "name": "general", "num_members": 5},
        {"id": "G0000002", "name": "direccion", "is_private": True},
    ],
}
USERS = {
    "ok": True,
    "members": [
        {"id": "U0000001", "name": "marta", "real_name": "Marta Ruiz", "profile": {}},
        {
            "id": "U0000002",
            "name": "pedro",
            "real_name": "Pedro Gil",
            "profile": {"display_name": "Pedro"},
        },
    ],
}


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


def route(routes):
    def handler(r):
        method = r.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=routes[method])

    return handler


def tools(creds=CREDS):
    return {t.spec.name: t for t in registry()["slack"].build_tools(creds)}


def test_manifest_link():
    q = parse_qs(urlparse(slack.MANIFEST_URL).query)
    assert q["new_app"] == ["1"]
    manifest = json.loads(q["manifest_json"][0])
    assert manifest["display_information"]["name"] == "Jarvis"
    assert "search:read" in manifest["oauth_config"]["scopes"]["user"]
    assert "chat:write" in manifest["oauth_config"]["scopes"]["user"]
    assert slack.MANIFEST_URL in registry()["slack"].steps[0]


def test_check(api):
    api.handler = route({"auth.test": {"ok": True, "team": "Acme", "user": "ana"}})
    assert registry()["slack"].check(CREDS) == "Acme · ana"
    req = api.calls[0]
    assert req.url.path == "/api/auth.test"
    assert req.headers["authorization"] == "Bearer xoxp-123"


def test_rejects_bot_and_bad_tokens(api):
    with pytest.raises(IntegrationError, match="xoxp-"):
        registry()["slack"].check({"token": "xoxb-999"})
    with pytest.raises(IntegrationError, match="xoxp-"):
        registry()["slack"].check({"token": "hola"})
    assert not api.calls
    api.handler = route({"auth.test": {"ok": False, "error": "invalid_auth"}})
    with pytest.raises(IntegrationError, match="no acepta el token"):
        registry()["slack"].check(CREDS)


def test_search(api):
    api.handler = route(
        {
            "search.messages": {
                "ok": True,
                "messages": {
                    "matches": [
                        {
                            "ts": "1899999000.000100",
                            "username": "marta",
                            "channel": {"name": "general"},
                            "text": "El presupuesto está listo",
                            "permalink": "https://acme.slack.com/p1",
                        }
                    ]
                },
            }
        }
    )
    data = json.loads(tools()["slack_search"].execute(texto="presupuesto").content)
    assert data == [
        {
            "fecha": "17/03 18:30",
            "de": "marta",
            "canal": "#general",
            "texto": "El presupuesto está listo",
            "enlace": "https://acme.slack.com/p1",
        }
    ]
    params = api.calls[0].url.params
    assert params["query"] == "presupuesto" and params["sort"] == "timestamp"


def test_read_channel_by_name(api):
    api.handler = route(
        {
            "conversations.list": CHANNELS,
            "users.list": USERS,
            "conversations.history": {
                "ok": True,
                "messages": [
                    {"ts": "1899999060", "user": "U0000002", "text": "Vale"},
                    {
                        "ts": "1899999000",
                        "user": "U0000001",
                        "text": "¿Reunión a las 5?",
                        "reply_count": 2,
                    },
                ],
            },
        }
    )
    out = tools()["slack_read_channel"].execute(canal="#general", limite=10)
    assert out.success, out.content
    assert out.content.splitlines() == [
        "17/03 18:30 Marta Ruiz: ¿Reunión a las 5? (2 respuestas)",
        "17/03 18:31 Pedro: Vale",
    ]
    hist = next(c for c in api.calls if c.url.path.endswith("conversations.history"))
    assert hist.url.params["channel"] == "C0000001"
    assert hist.url.params["limit"] == "10"


def test_unknown_channel_and_slack_error(api):
    api.handler = route({"conversations.list": CHANNELS})
    out = tools()["slack_read_channel"].execute(canal="ventas")
    assert not out.success and "No encuentro el canal" in out.content
    api.handler = route(
        {"conversations.history": {"ok": False, "error": "not_in_channel"}}
    )
    out = tools()["slack_read_channel"].execute(canal="C0000009")
    assert not out.success and "No estás dentro" in out.content


def test_list_channels(api):
    api.handler = route({"conversations.list": CHANNELS})
    data = json.loads(tools()["slack_list_channels"].execute().content)
    assert data == [
        {"id": "C0000001", "canal": "#general", "miembros": 5},
        {"id": "G0000002", "canal": "#direccion", "privado": True},
    ]
    assert api.calls[0].url.params["types"] == "public_channel,private_channel"


def test_send_message(api):
    api.handler = route(
        {
            "conversations.list": CHANNELS,
            "users.list": USERS,
            "chat.postMessage": {"ok": True, "ts": "1899999000.0001"},
        }
    )
    t = tools()["slack_send_message"]
    assert base.CONFIRM_NOTE in t.spec.description
    out = t.execute(destino="#general", texto="Hola equipo")
    assert out.success and out.content.startswith("Mensaje enviado a #general.")
    post = api.calls[-1]
    assert post.method == "POST" and post.url.path == "/api/chat.postMessage"
    assert json.loads(post.content) == {"channel": "C0000001", "text": "Hola equipo"}

    out = t.execute(destino="@Pedro Gil", texto="Llego tarde")
    assert out.success, out.content
    assert json.loads(api.calls[-1].content)["channel"] == "U0000002"
