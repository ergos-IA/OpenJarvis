"""Tests for the legal pages and the terms acceptance."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from openjarvis.server import legal_routes  # noqa: E402

KEY = "oj_sk_legal"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from openjarvis.server.app import create_app

    monkeypatch.setattr(legal_routes, "get_config_dir", lambda: tmp_path)
    for env in ("LEGAL_COMPANY", "LEGAL_NIF", "LEGAL_ADDRESS", "LEGAL_EMAIL"):
        monkeypatch.delenv(env, raising=False)
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["m"]
    return TestClient(create_app(engine, "m", api_key=KEY))


class TestPages:
    @pytest.mark.parametrize("doc", list(legal_routes.DOCS))
    def test_public_and_branded(self, client, doc):
        resp = client.get(f"/legal/{doc}")
        assert resp.status_code == 200
        assert "AI Fast Track" in resp.text
        assert "{{" not in resp.text  # every placeholder filled

    def test_unknown_doc(self, client):
        assert client.get("/legal/otro").status_code == 404

    def test_missing_company_data_is_flagged(self, client):
        text = client.get("/legal/aviso-legal").text
        assert "Borrador" in text and "[Razón social]" in text

    def test_company_data_filled_and_escaped(self, client, monkeypatch):
        values = {
            "LEGAL_COMPANY": "Edge <Expertise> SL",
            "LEGAL_NIF": "B12345678",
            "LEGAL_ADDRESS": "Calle Mayor 1, Madrid",
            "LEGAL_REGISTRY": "RM Madrid, T 1, F 1",
            "LEGAL_EMAIL": "legal@example.com",
            "LEGAL_JURISDICTION": "Madrid",
        }
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        text = client.get("/legal/aviso-legal").text
        assert "Edge &lt;Expertise&gt; SL" in text
        assert 'href="mailto:legal@example.com"' in text
        assert "Borrador" not in text

    def test_key_disclaimers_present(self, client):
        terms = client.get("/legal/terminos").text
        assert "no es un servicio oficial de Meta" in terms
        assert "dolo o culpa grave" in terms
        privacy = client.get("/legal/privacidad").text
        assert "encargado del tratamiento" in privacy and "aepd.es" in privacy


class TestAcceptance:
    def test_requires_key(self, client):
        assert client.get("/api/legal").status_code == 401

    def test_accept_flow(self, client):
        assert client.get("/api/legal", headers=AUTH).json()["accepted"] is False
        info = client.get("/api/jarvis/info", headers=AUTH).json()
        assert info["legal_accepted"] is False
        body = {
            "version": legal_routes.LEGAL_VERSION,
            "terms": True,
            "whatsapp_risk": True,
        }
        resp = client.post("/api/legal/accept", json=body, headers=AUTH)
        assert resp.status_code == 200 and resp.json()["accepted"] is True
        assert client.get("/api/jarvis/info", headers=AUTH).json()["legal_accepted"]

    def test_all_boxes_required(self, client):
        body = {
            "version": legal_routes.LEGAL_VERSION,
            "terms": True,
            "whatsapp_risk": False,
        }
        assert (
            client.post("/api/legal/accept", json=body, headers=AUTH).status_code == 400
        )

    def test_outdated_version_rejected(self, client):
        body = {"version": "2000-01-01", "terms": True, "whatsapp_risk": True}
        assert (
            client.post("/api/legal/accept", json=body, headers=AUTH).status_code == 409
        )

    def test_new_version_requires_acceptance_again(self, client, monkeypatch):
        body = {
            "version": legal_routes.LEGAL_VERSION,
            "terms": True,
            "whatsapp_risk": True,
        }
        client.post("/api/legal/accept", json=body, headers=AUTH)
        monkeypatch.setattr(legal_routes, "LEGAL_VERSION", "2099-01-01")
        status = client.get("/api/legal", headers=AUTH).json()
        assert (
            status["accepted"] is False and status["accepted_version"] != "2099-01-01"
        )
