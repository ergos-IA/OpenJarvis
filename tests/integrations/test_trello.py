"""Tests for the Trello integration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry

CREDS = {"key": "k" * 32, "token": "ATTAtok"}
BOARD = "5f0000000000000000000001"
BOARDS = [{"id": BOARD, "name": "Reforma", "shortUrl": "https://trello.com/b/x"}]


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


def tools():
    return {t.spec.name: t for t in registry()["trello"].build_tools(CREDS)}


def test_check_and_auth_params(api):
    api.handler = lambda r: httpx.Response(
        200, json={"fullName": "Ana López", "username": "ana"}
    )
    assert registry()["trello"].check(CREDS) == "Ana López"
    req = api.calls[0]
    assert req.url.host == "api.trello.com" and req.url.path == "/1/members/me"
    assert req.url.params["key"] == "k" * 32
    assert req.url.params["token"] == "ATTAtok"


def test_bad_token(api):
    api.handler = lambda r: httpx.Response(401, text="invalid token")
    with pytest.raises(IntegrationError, match="token"):
        registry()["trello"].check(CREDS)


def test_list_boards(api):
    api.handler = lambda r: httpx.Response(200, json=BOARDS)
    data = json.loads(tools()["trello_list_boards"].execute().content)
    assert data == [{"id": BOARD, "nombre": "Reforma", "url": "https://trello.com/b/x"}]
    assert api.calls[0].url.path == "/1/members/me/boards"
    assert api.calls[0].url.params["filter"] == "open"


def test_board_by_name(api):
    def handler(r):
        if r.url.path == "/1/members/me/boards":
            return httpx.Response(200, json=BOARDS)
        assert r.url.path == f"/1/boards/{BOARD}/lists"
        assert r.url.params["cards"] == "open"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "L1",
                    "name": "Pendiente",
                    "cards": [
                        {
                            "id": "C1",
                            "name": "Llamar al fontanero",
                            "due": "2030-03-04T09:00:00.000Z",
                            "dueComplete": False,
                            "desc": "",
                        }
                    ],
                },
                {"id": "L2", "name": "Hecho", "cards": []},
            ],
        )

    api.handler = handler
    out = tools()["trello_board"].execute(tablero="reforma")
    assert out.success, out.content
    data = json.loads(out.content)
    assert data[0] == {
        "lista": "Pendiente",
        "id": "L1",
        "tarjetas": [
            {"id": "C1", "nombre": "Llamar al fontanero", "vence": "04/03/2030 10:00"}
        ],
    }
    assert data[1]["tarjetas"] == []
    missing = tools()["trello_board"].execute(tablero="Otro")
    assert not missing.success and "No encuentro el tablero" in missing.content


def test_create_card(api):
    api.handler = lambda r: httpx.Response(
        200, json={"id": "C9", "shortUrl": "https://trello.com/c/9"}
    )
    out = tools()["trello_create_card"].execute(
        lista_id="L1", nombre="Comprar azulejos", vence="2030-03-10"
    )
    assert out.content == "Tarjeta creada: «Comprar azulejos» https://trello.com/c/9"
    req = api.calls[0]
    assert req.method == "POST" and req.url.path == "/1/cards"
    assert req.url.params["idList"] == "L1"
    assert req.url.params["name"] == "Comprar azulejos"
    assert req.url.params["due"] == "2030-03-10"
    assert "desc" not in req.url.params


def test_move_card(api):
    api.handler = lambda r: httpx.Response(200, json={"id": "C1", "name": "Fontanero"})
    out = tools()["trello_move_card"].execute(tarjeta_id="C1", lista_id="L2")
    assert out.content == "Tarjeta «Fontanero» movida."
    req = api.calls[0]
    assert req.method == "PUT" and req.url.path == "/1/cards/C1"
    assert req.url.params["idList"] == "L2"


def test_comment(api):
    api.handler = lambda r: httpx.Response(200, json={"id": "A1"})
    out = tools()["trello_comment"].execute(tarjeta_id="C1", texto="Viene el lunes")
    assert out.content == "Comentario añadido."
    req = api.calls[0]
    assert req.method == "POST" and req.url.path == "/1/cards/C1/actions/comments"
    assert req.url.params["text"] == "Viene el lunes"
    assert base.CONFIRM_NOTE in tools()["trello_comment"].spec.description
