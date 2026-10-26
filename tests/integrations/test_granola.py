"""Tests for the Granola integration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry

CREDS = {"token": "grn_KEY"}


def note(i, title, created="2030-03-04T09:00:00Z"):
    return {
        "id": f"not_{i:014d}",
        "object": "note",
        "title": title,
        "owner": {"name": "Ana", "email": "ana@ejemplo.es"},
        "created_at": created,
        "updated_at": created,
        "deleted_at": None,
    }


FULL = {
    **note(1, "Comité semanal"),
    "web_url": "https://notes.granola.ai/d/abc",
    "attendees": [
        {"name": "Luis", "email": "luis@x.es"},
        {"name": None, "email": "eva@x.es"},
    ],
    "summary_text": "Se aprobó el presupuesto.",
    "summary_markdown": "## Decisiones\n- Se aprobó el presupuesto",
    "transcript": [
        {"speaker": {"source": "microphone"}, "text": "Hola a todos"},
        {
            "speaker": {"source": "speaker", "diarization_label": "Speaker B"},
            "text": "Buenos días",
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


def tools_of(creds=CREDS):
    return {t.spec.name: t for t in registry()["granola"].build_tools(creds)}


def test_check_and_bad_key(api):
    api.handler = lambda r: httpx.Response(
        200, json={"notes": [note(1, "x")], "hasMore": False, "cursor": None}
    )
    assert registry()["granola"].check(CREDS) == "ana@ejemplo.es"
    req = api.calls[0]
    assert str(req.url) == "https://public-api.granola.ai/v1/notes?page_size=1"
    assert req.headers["authorization"] == "Bearer grn_KEY"

    api.handler = lambda r: httpx.Response(401, json={"error": "unauthorized"})
    with pytest.raises(IntegrationError, match="Granola no acepta"):
        registry()["granola"].check(CREDS)
    assert not tools_of()["granola_list_notes"].execute().success


def test_list_notes_paginates(api):
    pages = {
        None: {
            "notes": [note(1, "Comité semanal"), note(2, "Llamada con Pedro")],
            "hasMore": True,
            "cursor": "c2",
        },
        "c2": {
            "notes": [
                dict(note(3, "Borrada"), deleted_at="2030-03-05T00:00:00Z"),
                note(4, "Demo"),
            ],
            "hasMore": False,
            "cursor": None,
        },
    }
    api.handler = lambda r: httpx.Response(200, json=pages[r.url.params.get("cursor")])
    out = json.loads(tools_of()["granola_list_notes"].execute(limite=4, dias=7).content)
    assert [n["titulo"] for n in out] == ["Comité semanal", "Llamada con Pedro", "Demo"]
    assert out[0]["fecha"] == "lun 04/03/2030 10:00"
    assert out[0]["id"] == "not_00000000000001"
    first, second = api.calls
    assert first.url.params["page_size"] == "4" and "created_after" in first.url.params
    assert second.url.params["cursor"] == "c2" and second.url.params["page_size"] == "2"

    api.handler = lambda r: httpx.Response(200, json={"notes": [], "hasMore": False})
    assert "No hay notas" in tools_of()["granola_list_notes"].execute().content


def test_search_by_title_and_summary(api):
    listing = {
        "notes": [note(1, "Comité semanal"), note(2, "Llamada con Pedro")],
        "hasMore": False,
    }

    def handler(req):
        if req.url.path == "/v1/notes":
            return httpx.Response(200, json=listing)
        if req.url.path.endswith("00000000000001"):
            return httpx.Response(200, json=FULL)
        return httpx.Response(
            200,
            json={
                **note(2, "Llamada con Pedro"),
                "summary_text": "Hablamos de vacaciones",
            },
        )

    api.handler = handler
    t = tools_of()["granola_search_notes"]
    out = json.loads(t.execute(texto="pedro", limite=1).content)
    assert [n["titulo"] for n in out] == ["Llamada con Pedro"]

    out = json.loads(t.execute(texto="presupuesto").content)
    assert [n["titulo"] for n in out] == ["Comité semanal"]

    assert "No encuentro" in t.execute(texto="bicicleta").content
    assert not t.execute(texto=" ").success


def test_read_note(api):
    api.handler = lambda r: httpx.Response(200, json=FULL)
    out = tools_of()["granola_read_note"].execute(id="not_00000000000001").content
    req = api.calls[-1]
    assert req.url.path == "/v1/notes/not_00000000000001"
    assert req.url.params["include"] == "transcript"
    assert out.startswith("# Comité semanal")
    assert "Asistentes: Luis, eva@x.es" in out
    assert "- Se aprobó el presupuesto" in out
    assert "Yo: Hola a todos" in out and "Speaker B: Buenos días" in out
    assert "https://notes.granola.ai/d/abc" in out


def test_read_note_transcript_too_large(api):
    def handler(req):
        if req.url.params.get("include"):
            return httpx.Response(413, json={"error": "TRANSCRIPT_TOO_LARGE"})
        return httpx.Response(200, json={**FULL, "transcript": None})

    api.handler = handler
    out = tools_of()["granola_read_note"].execute(id="not_00000000000001").content
    assert "demasiado larga" in out and "Se aprobó" in out
    assert len(api.calls) == 2
