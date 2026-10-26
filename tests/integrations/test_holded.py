"""Tests for the Holded integration."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry
from openjarvis.integrations.providers import holded

CREDS = {"token": "hk_123"}
NOW = int(time.time())
DAY = 86400

INVOICES = [
    {
        "id": "inv1",
        "docNumber": "F2030-001",
        "contactName": "Construcciones Pérez SL",
        "date": NOW - 40 * DAY,
        "dueDate": NOW - 10 * DAY,
        "total": 1210.0,
        "subtotal": 1000.0,
        "tax": 210.0,
        "paymentsTotal": 0,
        "paymentsPending": 1210.0,
        "desc": "Reforma baño",
    },
    {
        "id": "inv2",
        "docNumber": "F2030-002",
        "contactName": "Ana López",
        "date": NOW - 5 * DAY,
        "dueDate": NOW + 25 * DAY,
        "total": 605.0,
        "subtotal": 500.0,
        "paymentsTotal": 100.0,
        "paymentsPending": 505.0,
    },
    {
        "id": "inv3",
        "docNumber": "F2030-003",
        "contactName": "Bar Manolo",
        "date": NOW - 3 * DAY,
        "dueDate": NOW - DAY,
        "total": 242.0,
        "subtotal": 200.0,
        "paymentsTotal": 242.0,
        "paymentsPending": 0,
    },
]


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
    return {t.spec.name: t for t in registry()["holded"].build_tools(creds)}


def test_registered():
    integ = registry()["holded"]
    assert integ.category == "negocio"
    assert integ.fields[0].key == "token"
    names = set(tools_of())
    assert names == {
        "holded_list_invoices",
        "holded_get_invoice",
        "holded_search_contacts",
        "holded_create_contact",
        "holded_list_products",
        "holded_summary",
    }
    assert "confirme" in tools_of()["holded_create_contact"].spec.description


def test_eur_format():
    assert holded.eur(1234.5) == "1.234,50 €"
    assert holded.eur(None) == "0,00 €"


def test_check_and_bad_key(api):
    api.handler = lambda r: httpx.Response(200, json=[])
    assert registry()["holded"].check(CREDS) == "Cuenta de Holded"
    req = api.calls[0]
    assert req.headers["key"] == "hk_123"
    assert req.url.path == "/api/invoicing/v1/documents/invoice"
    assert int(req.url.params["endtmp"]) > int(req.url.params["starttmp"])

    api.handler = lambda r: httpx.Response(401, json={"status": 0})
    with pytest.raises(IntegrationError, match="API key"):
        registry()["holded"].check(CREDS)
    out = tools_of()["holded_list_products"].execute()
    assert not out.success and "API key" in out.content


def test_list_invoices_filters(api):
    api.handler = lambda r: httpx.Response(200, json=INVOICES)
    t = tools_of()["holded_list_invoices"]

    data = json.loads(t.execute(estado="pendientes").content)
    nums = [f["numero"] for f in data["facturas"]]
    assert nums == ["F2030-001", "F2030-002"]
    assert "1.715,00 €" in data["resumen"]
    first = data["facturas"][0]
    assert first["estado"] == "vencida" and first["total"] == "1.210,00 €"
    assert data["facturas"][1]["estado"] == "cobrada en parte"
    assert data["facturas"][1]["pendiente"] == "505,00 €"

    data = json.loads(t.execute(estado="vencidas").content)
    assert [f["numero"] for f in data["facturas"]] == ["F2030-001"]

    data = json.loads(t.execute(desde="2030-01-01", hasta="2030-01-31").content)
    assert len(data["facturas"]) == 3
    assert data["facturas"][2]["estado"] == "cobrada"
    params = api.calls[-1].url.params
    from datetime import datetime

    from openjarvis.personal.reminders import local_tz

    start = datetime.fromtimestamp(int(params["starttmp"]), local_tz())
    end = datetime.fromtimestamp(int(params["endtmp"]), local_tz())
    assert (start.day, start.month, end.day, end.hour) == (1, 1, 31, 23)

    out = t.execute(cliente="manolo", estado="pendientes").content
    assert "No hay facturas" in out
    assert not t.execute(desde="ayer").success


def test_get_invoice(api):
    doc = dict(
        INVOICES[0],
        products=[{"name": "Azulejos", "units": 10, "price": 100, "tax": 21}],
    )
    api.handler = lambda r: httpx.Response(200, json=doc)
    out = json.loads(tools_of()["holded_get_invoice"].execute(id="inv1").content)
    assert api.calls[0].url.path.endswith("/documents/invoice/inv1")
    assert out["base_imponible"] == "1.000,00 €"
    assert out["lineas"] == ["10 × Azulejos a 100,00 € (IVA 21%)"]


def test_contacts_search_and_create(api):
    contacts = [
        {
            "id": "c1",
            "name": "Construcciones Pérez SL",
            "code": "B12345678",
            "mobile": "600111222",
        },
        {"id": "c2", "name": "Ana López", "email": "ana@ejemplo.es"},
    ]
    api.handler = lambda r: httpx.Response(200, json=contacts)
    out = json.loads(
        tools_of()["holded_search_contacts"].execute(texto="b1234").content
    )
    assert out == [
        {
            "id": "c1",
            "nombre": "Construcciones Pérez SL",
            "nif": "B12345678",
            "telefono": "600111222",
        }
    ]
    assert (
        "No encuentro"
        in tools_of()["holded_search_contacts"].execute(texto="zzz").content
    )

    api.handler = lambda r: httpx.Response(
        200, json={"status": 1, "info": "Created", "id": "c9"}
    )
    res = tools_of()["holded_create_contact"].execute(
        nombre="Talleres Ruiz", nif="B87654321", email="hola@ruiz.es", ciudad="Valencia"
    )
    assert res.success and "c9" in res.content
    req = api.calls[-1]
    assert req.method == "POST" and req.url.path == "/api/invoicing/v1/contacts"
    body = json.loads(req.content)
    assert body == {
        "name": "Talleres Ruiz",
        "type": "client",
        "code": "B87654321",
        "email": "hola@ruiz.es",
        "billAddress": {"city": "Valencia", "country": "España"},
    }


def test_products(api):
    api.handler = lambda r: httpx.Response(
        200,
        json=[
            {
                "id": "p1",
                "name": "Hora de consultoría",
                "price": 60,
                "tax": 21,
                "kind": "service",
                "stock": 0,
            },
            {
                "id": "p2",
                "name": "Cable USB",
                "sku": "USB-1",
                "price": 4.5,
                "stock": 12,
            },
        ],
    )
    out = json.loads(tools_of()["holded_list_products"].execute(texto="usb").content)
    assert out == [
        {
            "id": "p2",
            "nombre": "Cable USB",
            "referencia": "USB-1",
            "precio": "4,50 €",
            "stock": 12,
        }
    ]
    assert api.calls[0].url.path == "/api/invoicing/v1/products"


def test_summary(api):
    api.handler = lambda r: httpx.Response(200, json=INVOICES)
    out = tools_of()["holded_summary"].execute(periodo="año").content
    assert "Facturas emitidas: 3" in out
    assert "Total facturado: 2.057,00 €" in out
    assert "Pendiente de cobro: 1.715,00 €" in out
    assert "Cobrado: 342,00 €" in out
    assert "vencido: 1.210,00 € en 1 factura" in out
    out = tools_of()["holded_summary"].execute(periodo="2030-02").content
    assert "02/2030" in out and "29/02/2030" not in out and "28/02/2030" in out
    assert not tools_of()["holded_summary"].execute(periodo="semana").success
