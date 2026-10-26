"""Tests for the Dropbox OAuth integration."""

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
def api():
    holder = SimpleNamespace(handler=lambda r: httpx.Response(404), calls=[])

    def dispatch(request):
        holder.calls.append(request)
        return holder.handler(request)

    base.set_http(httpx.Client(transport=httpx.MockTransport(dispatch)))
    yield holder
    base.set_http(None)


CREDS = Creds({}, lambda: "tok")


def tools():
    return {t.spec.name: t for t in registry()["dropbox"].build_tools(CREDS)}


FILE = {
    ".tag": "file",
    "id": "id:abc",
    "name": "Factura luz.pdf",
    "path_display": "/Facturas/Factura luz.pdf",
    "size": 254000,
    "server_modified": "2030-03-01T10:00:00Z",
}


def test_oauth_spec(tmp_path, monkeypatch):
    spec = registry()["dropbox"].oauth
    assert spec.client_env == "DROPBOX"
    assert spec.extra_params == {"token_access_type": "offline"}
    assert set(spec.scopes) == {
        "files.metadata.read",
        "files.content.read",
        "account_info.read",
    }
    assert spec.token_url == "https://api.dropboxapi.com/oauth2/token"
    monkeypatch.setenv("OPENJARVIS_DROPBOX_CLIENT_ID", "k")
    monkeypatch.setenv("OPENJARVIS_DROPBOX_CLIENT_SECRET", "s")
    url = IntegrationManager(tmp_path / "i.json").begin_oauth("dropbox", "h")
    params = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.dropbox.com/oauth2/authorize?")
    assert params["token_access_type"] == ["offline"]


def test_check_and_401(api):
    def handler(r):
        assert r.method == "POST" and r.url.path == "/2/users/get_current_account"
        assert r.headers["Authorization"] == "Bearer tok" and not r.content
        return httpx.Response(
            200, json={"email": "ana@x.com", "name": {"display_name": "Ana"}}
        )

    api.handler = handler
    assert registry()["dropbox"].check(CREDS) == "ana@x.com"
    api.handler = lambda r: httpx.Response(
        401, json={"error_summary": "expired_access_token/"}
    )
    with pytest.raises(IntegrationError, match="Vuelve a conectar Dropbox"):
        registry()["dropbox"].check(CREDS)
    assert not tools()["dropbox_list_folder"].execute().success


def test_search(api):
    def handler(r):
        body = json.loads(r.content)
        assert r.url.path == "/2/files/search_v2"
        assert body == {
            "query": "factura",
            "options": {
                "max_results": 5,
                "file_status": "active",
                "filename_only": True,
                "path": "/Facturas",
            },
        }
        return httpx.Response(
            200,
            json={"matches": [{"metadata": {".tag": "metadata", "metadata": FILE}}]},
        )

    api.handler = handler
    out = json.loads(
        tools()["dropbox_search"]
        .execute(texto="factura", solo_nombre=True, carpeta="Facturas", limite=5)
        .content
    )
    assert out == [
        {
            "nombre": "Factura luz.pdf",
            "ruta": "/Facturas/Factura luz.pdf",
            "tipo": "archivo",
            "tamaño": "248.0 KB",
            "modificado": "2030-03-01",
        }
    ]
    api.handler = lambda r: httpx.Response(200, json={"matches": []})
    assert "No encuentro" in tools()["dropbox_search"].execute(texto="x").content


def test_list_folder(api):
    def handler(r):
        assert json.loads(r.content) == {"path": "", "limit": 100}
        return httpx.Response(
            200,
            json={
                "entries": [
                    FILE,
                    {".tag": "folder", "name": "Trabajo", "path_display": "/Trabajo"},
                ],
                "has_more": True,
            },
        )

    api.handler = handler
    out = json.loads(tools()["dropbox_list_folder"].execute(carpeta="/").content)
    assert out[0] == {"nombre": "Trabajo", "ruta": "/Trabajo", "tipo": "carpeta"}
    assert out[1]["nombre"] == "Factura luz.pdf" and "nota" in out[2]


def test_read_text_binary_and_missing(api):
    def handler(r):
        if r.url.path == "/2/files/get_metadata":
            path = json.loads(r.content)["path"]
            if path == "/notas.txt":
                return httpx.Response(
                    200,
                    json={
                        ".tag": "file",
                        "id": "id:n",
                        "name": "notas.txt",
                        "size": 20,
                    },
                )
            if path == "/Facturas/Factura luz.pdf":
                return httpx.Response(200, json=FILE)
            return httpx.Response(
                409, json={"error_summary": "path/not_found/.", "error": {}}
            )
        if r.url.host == "content.dropboxapi.com":
            assert r.url.path == "/2/files/download"
            assert json.loads(r.headers["Dropbox-API-Arg"]) == {"path": "id:n"}
            return httpx.Response(
                200,
                content="Comprar pan y café".encode(),
                headers={"content-type": "application/octet-stream"},
            )
        return httpx.Response(404)

    api.handler = handler
    t = tools()
    assert (
        t["dropbox_read"].execute(ruta="notas.txt").content
        == "# notas.txt\nComprar pan y café"
    )
    pdf = t["dropbox_read"].execute(ruta="/Facturas/Factura luz.pdf").content
    assert "PDF" in pdf and "248.0 KB" in pdf and "dropbox_link" in pdf
    res = t["dropbox_read"].execute(ruta="/nada.txt")
    assert not res.success and "No encuentro" in res.content


def test_temporary_link(api):
    def handler(r):
        assert r.url.path == "/2/files/get_temporary_link"
        assert json.loads(r.content) == {"path": "/Facturas/Factura luz.pdf"}
        return httpx.Response(
            200, json={"metadata": FILE, "link": "https://dl.dropboxusercontent.com/x"}
        )

    api.handler = handler
    out = tools()["dropbox_link"].execute(ruta="/Facturas/Factura luz.pdf").content
    assert "https://dl.dropboxusercontent.com/x" in out and "Factura luz.pdf" in out
