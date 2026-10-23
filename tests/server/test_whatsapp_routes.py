"""Tests for the WhatsApp (Baileys) pairing page and /api/whatsapp endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel  # noqa: E402


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["test-model"]
    return engine


def _client(engine, bridge=None, api_key=""):
    from starlette.testclient import TestClient

    from openjarvis.server.app import create_app

    return TestClient(
        create_app(engine, "test-model", channel_bridge=bridge, api_key=api_key)
    )


def _wrapped(channel):
    """Mimic serve.py, which wraps the channel in a ChannelBridge."""
    bridge = MagicMock()
    bridge.channel_id = ""
    bridge._channels = {"whatsapp_baileys": channel}
    return bridge


class TestStatus:
    def test_disabled_without_channel(self, mock_engine):
        resp = _client(mock_engine).get("/api/whatsapp/status")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}

    def test_reports_qr_while_pairing(self, mock_engine):
        ch = WhatsAppBaileysChannel()
        ch._handle_bridge_event({"type": "qr", "data": "abc", "svg": "<svg/>"})
        data = _client(mock_engine, _wrapped(ch)).get("/api/whatsapp/status").json()
        assert data["enabled"] is True
        assert data["connected"] is False
        assert data["qr_svg"] == "<svg/>"
        assert data["mode"] == "self"

    def test_reports_connected_number(self, mock_engine):
        ch = WhatsAppBaileysChannel()
        ch._handle_bridge_event({"type": "qr", "data": "abc", "svg": "<svg/>"})
        ch._handle_bridge_event(
            {"type": "status", "status": "connected", "me": "34600111222"}
        )
        data = _client(mock_engine, _wrapped(ch)).get("/api/whatsapp/status").json()
        assert data["connected"] is True
        assert data["me"] == "34600111222"
        assert data["qr_svg"] == ""

    def test_requires_api_key_when_configured(self, mock_engine):
        ch = WhatsAppBaileysChannel()
        client = _client(mock_engine, _wrapped(ch), api_key="secret")
        assert client.get("/api/whatsapp/status").status_code == 401
        ok = client.get(
            "/api/whatsapp/status", headers={"Authorization": "Bearer secret"}
        )
        assert ok.status_code == 200


class TestLogout:
    def test_logout_sends_command(self, mock_engine):
        ch = WhatsAppBaileysChannel()
        ch._process = MagicMock()
        resp = _client(mock_engine, _wrapped(ch)).post("/api/whatsapp/logout")
        assert resp.status_code == 200
        written = ch._process.stdin.write.call_args[0][0]
        assert '"type":"logout"' in written

    def test_logout_without_channel_is_404(self, mock_engine):
        resp = _client(mock_engine).post("/api/whatsapp/logout")
        assert resp.status_code == 404

    def test_logout_without_process_is_503(self, mock_engine):
        ch = WhatsAppBaileysChannel()
        resp = _client(mock_engine, _wrapped(ch)).post("/api/whatsapp/logout")
        assert resp.status_code == 503


class TestPage:
    def test_page_is_public(self, mock_engine):
        resp = _client(mock_engine, api_key="secret").get("/whatsapp")
        assert resp.status_code == 200
        assert "Vincular WhatsApp" in resp.text
