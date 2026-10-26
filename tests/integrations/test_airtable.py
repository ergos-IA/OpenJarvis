"""Tests for the Airtable integration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry

CREDS = {"token": "patABC.123"}

SCHEMA = {
    "tables": [
        {
            "id": "tblClientes",
            "name": "Clientes",
            "fields": [
                {"id": "f1", "name": "Nombre", "type": "singleLineText"},
                {"id": "f2", "name": "Estado", "type": "singleSelect"},
            ],
        }
    ]
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


def tools_of(creds=CREDS):
    return {t.spec.name: t for t in registry()["airtable"].build_tools(creds)}


def test_registered():
    tools = tools_of()
    assert set(tools) == {
        "airtable_list_bases",
        "airtable_list_tables",
        "airtable_search_records",
        "airtable_create_record",
        "airtable_update_record",
    }
    assert tools["airtable_update_record"].spec.metadata["writes"] is True
    assert (
        tools["airtable_create_record"].spec.parameters["properties"]["campos"]["type"]
        == "object"
    )


def test_check_and_bad_token(api):
    api.handler = lambda r: httpx.Response(
        200, json={"id": "usr1", "email": "ana@ejemplo.es"}
    )
    assert registry()["airtable"].check(CREDS) == "ana@ejemplo.es"
    req = api.calls[0]
    assert str(req.url) == "https://api.airtable.com/v0/meta/whoami"
    assert req.headers["authorization"] == "Bearer patABC.123"

    api.handler = lambda r: httpx.Response(200, json={"id": "usr1"})
    assert registry()["airtable"].check(CREDS) == "Airtable"

    api.handler = lambda r: httpx.Response(
        401, json={"error": "AUTHENTICATION_REQUIRED"}
    )
    with pytest.raises(IntegrationError, match="Airtable no acepta"):
        registry()["airtable"].check(CREDS)


def test_bases_and_tables(api):
    api.handler = lambda r: httpx.Response(
        200,
        json={"bases": [{"id": "appX", "name": "CRM", "permissionLevel": "create"}]},
    )
    out = json.loads(tools_of()["airtable_list_bases"].execute().content)
    assert out == [{"id": "appX", "nombre": "CRM"}]

    api.handler = lambda r: httpx.Response(200, json={"bases": []})
    assert "Access" in tools_of()["airtable_list_bases"].execute().content

    api.handler = lambda r: httpx.Response(200, json=SCHEMA)
    out = json.loads(tools_of()["airtable_list_tables"].execute(base_id="appX").content)
    assert out == [
        {
            "id": "tblClientes",
            "tabla": "Clientes",
            "campos": "Nombre (singleLineText), Estado (singleSelect)",
        }
    ]
    assert api.calls[-1].url.path == "/v0/meta/bases/appX/tables"


def test_search_records(api):
    records = {
        "records": [
            {
                "id": "rec1",
                "fields": {
                    "Nombre": "Marta López",
                    "Estado": "Pendiente",
                    "Etiquetas": ["vip", "web"],
                    "Foto": [{"url": "https://x/1.png", "filename": "1.png"}],
                },
            }
        ]
    }

    def handler(req):
        if req.url.path.startswith("/v0/meta/"):
            return httpx.Response(200, json=SCHEMA)
        return httpx.Response(200, json=records)

    api.handler = handler
    t = tools_of()["airtable_search_records"]
    out = json.loads(
        t.execute(base_id="appX", tabla="Clientes", texto='Mar"ta').content
    )
    assert out == [
        {
            "id": "rec1",
            "Nombre": "Marta López",
            "Estado": "Pendiente",
            "Etiquetas": "vip, web",
            "Foto": "1.png",
        }
    ]
    req = api.calls[-1]
    assert req.url.path == "/v0/appX/Clientes"
    assert req.url.params["maxRecords"] == "20"
    find = 'FIND("mar\\"ta", LOWER({%s}&\'\'))'
    assert req.url.params["filterByFormula"] == (
        f"OR({find % 'Nombre'}, {find % 'Estado'})"
    )

    t.execute(
        base_id="appX",
        tabla="Mis clientes",
        formula="{Estado}='Pendiente'",
        vista="Abiertos",
        limite=500,
    )
    req = api.calls[-1]
    assert req.url.raw_path.startswith(b"/v0/appX/Mis%20clientes?")
    assert req.url.params["filterByFormula"] == "{Estado}='Pendiente'"
    assert req.url.params["view"] == "Abiertos"
    assert req.url.params["maxRecords"] == "100"

    bad = t.execute(base_id="appX", tabla="Proveedores", texto="x")
    assert not bad.success and "Proveedores" in bad.content

    api.handler = lambda r: httpx.Response(200, json={"records": []})
    assert "No hay registros" in t.execute(base_id="appX", tabla="Clientes").content


def test_create_and_update(api):
    api.handler = lambda r: httpx.Response(
        200, json={"records": [{"id": "recNEW", "fields": {}}]}
    )
    out = tools_of()["airtable_create_record"].execute(
        base_id="appX",
        tabla="Clientes",
        campos={"Nombre": "Marta", "Estado": "Pendiente"},
    )
    assert out.content == "Registro creado en «Clientes» (id recNEW)."
    req = api.calls[-1]
    assert req.method == "POST" and req.url.path == "/v0/appX/Clientes"
    assert json.loads(req.content) == {
        "records": [{"fields": {"Nombre": "Marta", "Estado": "Pendiente"}}],
        "typecast": True,
    }

    # Fields as a JSON string also work.
    out = tools_of()["airtable_update_record"].execute(
        base_id="appX",
        tabla="Clientes",
        registro_id="rec1",
        campos='{"Estado": "Hecho"}',
    )
    assert out.success and "rec1" in out.content
    req = api.calls[-1]
    assert req.method == "PATCH"
    assert json.loads(req.content) == {
        "records": [{"id": "rec1", "fields": {"Estado": "Hecho"}}],
        "typecast": True,
    }

    bad = tools_of()["airtable_update_record"].execute(
        base_id="appX", tabla="Clientes", registro_id="rec1", campos="no es json"
    )
    assert not bad.success and "objeto" in bad.content
