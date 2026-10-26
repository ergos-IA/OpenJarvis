"""Tests for the HubSpot integration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry

CREDS = {"token": "pat-eu1-abc"}

PIPELINES = {
    "results": [
        {
            "id": "default",
            "stages": [
                {"id": "appointmentscheduled", "label": "Cita programada"},
                {"id": "contractsent", "label": "Contrato enviado"},
                {"id": "closedwon", "label": "Cerrado ganado"},
            ],
        },
        {"id": "777", "stages": [{"id": "999", "label": "Reformas: visita"}]},
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
    return {t.spec.name: t for t in registry()["hubspot"].build_tools(creds)}


def body_of(req):
    return json.loads(req.content)


def test_registered():
    integ = registry()["hubspot"]
    assert integ.category == "negocio"
    tools = tools_of()
    assert set(tools) == {
        "hubspot_search_contacts",
        "hubspot_search_companies",
        "hubspot_list_deals",
        "hubspot_create_contact",
        "hubspot_create_deal",
        "hubspot_add_note",
    }
    for name in ("hubspot_create_contact", "hubspot_create_deal", "hubspot_add_note"):
        assert tools[name].spec.metadata["writes"] is True
    assert tools["hubspot_search_contacts"].spec.metadata["writes"] is False


def test_check_and_bad_token(api):
    api.handler = lambda r: httpx.Response(
        200, json={"portalId": 4242, "accountType": "STANDARD"}
    )
    assert registry()["hubspot"].check(CREDS) == "Cuenta HubSpot 4242"
    req = api.calls[0]
    assert req.url.path == "/account-info/v3/details"
    assert req.headers["authorization"] == "Bearer pat-eu1-abc"

    api.handler = lambda r: httpx.Response(401, json={"status": "error"})
    with pytest.raises(IntegrationError, match="permisos"):
        registry()["hubspot"].check(CREDS)
    out = tools_of()["hubspot_search_contacts"].execute(texto="ana")
    assert not out.success and "HubSpot no acepta" in out.content


def test_search_contacts_and_companies(api):
    def handler(req):
        if "contacts" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "51",
                            "properties": {
                                "firstname": "Laura",
                                "lastname": "Gómez",
                                "email": "laura@gomez.es",
                                "phone": None,
                                "company": "Gómez SL",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "9",
                        "properties": {"name": "Gómez SL", "domain": "gomez.es"},
                    }
                ]
            },
        )

    api.handler = handler
    out = json.loads(
        tools_of()["hubspot_search_contacts"].execute(texto="laura", limite=5).content
    )
    assert out == [
        {
            "id": "51",
            "nombre": "Laura Gómez",
            "email": "laura@gomez.es",
            "empresa": "Gómez SL",
        }
    ]
    req = api.calls[-1]
    assert req.method == "POST" and req.url.path == "/crm/v3/objects/contacts/search"
    body = body_of(req)
    assert (
        body["query"] == "laura"
        and body["limit"] == 5
        and "email" in body["properties"]
    )

    out = json.loads(
        tools_of()["hubspot_search_companies"].execute(texto="gomez").content
    )
    assert out == [{"id": "9", "nombre": "Gómez SL", "web": "gomez.es"}]
    assert api.calls[-1].url.path == "/crm/v3/objects/companies/search"

    api.handler = lambda r: httpx.Response(200, json={"results": []})
    assert "No he encontrado" in tools_of()["hubspot_search_contacts"].execute().content


def test_list_deals_with_stage(api):
    def handler(req):
        if req.url.path == "/crm/v3/pipelines/deals":
            return httpx.Response(200, json=PIPELINES)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "300",
                        "properties": {
                            "dealname": "Reforma cocina",
                            "amount": "12500.5",
                            "dealstage": "contractsent",
                            "closedate": "2030-05-01T10:00:00Z",
                        },
                    }
                ]
            },
        )

    api.handler = handler
    out = json.loads(tools_of()["hubspot_list_deals"].execute(etapa="contrato").content)
    assert out == [
        {
            "id": "300",
            "negocio": "Reforma cocina",
            "importe": "12.500,50 €",
            "etapa": "Contrato enviado",
            "cierre": "01/05/2030",
        }
    ]
    body = body_of(api.calls[-1])
    assert body["filterGroups"] == [
        {
            "filters": [
                {
                    "propertyName": "dealstage",
                    "operator": "IN",
                    "values": ["contractsent"],
                }
            ]
        }
    ]
    assert "query" not in body


def test_create_contact_and_conflict(api):
    api.handler = lambda r: httpx.Response(201, json={"id": "77", "properties": {}})
    out = tools_of()["hubspot_create_contact"].execute(
        nombre="Pedro", apellidos="Ruiz", email="pedro@ruiz.es"
    )
    assert out.content == "Contacto «Pedro Ruiz» creado en HubSpot (id 77)."
    req = api.calls[-1]
    assert req.method == "POST" and req.url.path == "/crm/v3/objects/contacts"
    assert body_of(req) == {
        "properties": {
            "firstname": "Pedro",
            "lastname": "Ruiz",
            "email": "pedro@ruiz.es",
        }
    }

    api.handler = lambda r: httpx.Response(
        409,
        json={
            "status": "error",
            "message": "Contact already exists",
            "category": "CONFLICT",
        },
    )
    out = tools_of()["hubspot_create_contact"].execute(
        nombre="Pedro", email="pedro@ruiz.es"
    )
    assert out.success and "Ya existe" in out.content


def test_create_deal(api):
    def handler(req):
        if req.url.path == "/crm/v3/pipelines/deals":
            return httpx.Response(200, json=PIPELINES)
        return httpx.Response(201, json={"id": "501"})

    api.handler = handler
    out = tools_of()["hubspot_create_deal"].execute(
        nombre="Reforma baño García",
        importe="1.500,50 €",
        etapa="visita",
        fecha_cierre="2030-06-30",
        contacto_id="51",
    )
    assert out.content == "Negocio «Reforma baño García» creado en HubSpot (id 501)."
    req = api.calls[-1]
    assert req.url.path == "/crm/v3/objects/deals"
    body = body_of(req)
    assert body["properties"] == {
        "dealname": "Reforma baño García",
        "pipeline": "777",
        "amount": "1500.5",
        "dealstage": "999",
        "closedate": "2030-06-30T00:00:00Z",
    }
    assert body["associations"][0]["to"] == {"id": "51"}
    assert body["associations"][0]["types"][0]["associationTypeId"] == 3

    # No stage: first stage of the default pipeline.
    tools_of()["hubspot_create_deal"].execute(nombre="Otro")
    props = body_of(api.calls[-1])["properties"]
    assert (
        props["dealstage"] == "appointmentscheduled" and props["pipeline"] == "default"
    )
    assert (
        not tools_of()["hubspot_create_deal"]
        .execute(nombre="X", importe="mucho")
        .success
    )


def test_add_note(api):
    api.handler = lambda r: httpx.Response(201, json={"id": "n1"})
    out = tools_of()["hubspot_add_note"].execute(
        contacto_id="51", texto="Llamada: quiere presupuesto"
    )
    assert out.success and "Nota añadida" in out.content
    req = api.calls[-1]
    assert req.url.path == "/crm/v3/objects/notes"
    body = body_of(req)
    assert body["properties"]["hs_note_body"] == "Llamada: quiere presupuesto"
    assert body["properties"]["hs_timestamp"].endswith("Z")
    assert body["associations"][0]["types"][0] == {
        "associationCategory": "HUBSPOT_DEFINED",
        "associationTypeId": 202,
    }
    assert (
        not tools_of()["hubspot_add_note"].execute(contacto_id="51", texto=" ").success
    )
