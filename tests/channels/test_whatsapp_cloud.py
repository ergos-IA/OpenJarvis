"""Tests for the WhatsApp Business (Meta Cloud API) channel and its routes."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.channels import whatsapp_baileys as wb
from openjarvis.channels._stubs import ChannelStatus
from openjarvis.channels.whatsapp_cloud import WhatsAppCloudChannel
from openjarvis.channels.whatsapp_store import WhatsAppStore

OWNER = "34600111222"
SECRET = "app-secret"


class FakeMeta:
    def __init__(self):
        self.sent = []
        self.fail_window = False

    def __call__(self, request: httpx.Request):
        if request.method == "GET":
            if request.headers["authorization"] != "Bearer good":
                return httpx.Response(
                    401, json={"error": {"message": "Invalid OAuth access token"}}
                )
            return httpx.Response(
                200,
                json={
                    "display_phone_number": "+34 910 000 000",
                    "verified_name": "Jarvis",
                },
            )
        body = json.loads(request.content)
        self.sent.append(body)
        if self.fail_window and body["type"] == "text":
            return httpx.Response(
                400,
                json={"error": {"code": 131047, "message": "Re-engagement message"}},
            )
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
def channel(tmp_path, meta, monkeypatch):
    for env in (
        "META_WA_PHONE_NUMBER_ID",
        "META_WA_ACCESS_TOKEN",
        "META_WA_APP_SECRET",
        "WHATSAPP_OWNER_NUMBER",
        "META_WA_TEMPLATE",
    ):
        monkeypatch.delenv(env, raising=False)
    store = WhatsAppStore(tmp_path / "wa.db")
    ch = WhatsAppCloudChannel(
        config_path=tmp_path / "cloud.json",
        http=httpx.Client(transport=httpx.MockTransport(meta)),
        store=store,
    )
    yield ch
    store.close()
    if wb._active_channel is ch:
        wb._active_channel = None


def _configure(ch, **extra):
    return ch.configure(
        phone_number_id="555",
        access_token="good",
        owner_number="600 111 222",
        app_secret=SECRET,
        **extra,
    )


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _webhook(sender: str, text: str, name: str = "Ana") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "555"},
                            "contacts": [{"wa_id": sender, "profile": {"name": name}}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": "wamid.in",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


class TestSetup:
    def test_verify_token_generated_and_kept(self, channel, tmp_path, meta):
        token = channel.verify_token
        assert len(token) > 20
        again = WhatsAppCloudChannel(
            config_path=tmp_path / "cloud.json", http=MagicMock()
        )
        assert again.verify_token == token

    def test_configure_validates_with_meta(self, channel):
        with pytest.raises(ValueError, match="Invalid OAuth"):
            channel.configure(
                phone_number_id="555", access_token="bad", owner_number=OWNER
            )
        info = _configure(channel)
        assert info["connected"] and info["provider"] == "meta"
        assert info["me"] == "34910000000" and info["owner"] == OWNER  # +34 added
        assert channel.self_jid() == f"{OWNER}@s.whatsapp.net"
        assert "access_token" not in info

    def test_resave_keeps_token(self, channel):
        _configure(channel)
        channel.configure(phone_number_id="555", access_token="", owner_number=OWNER)
        assert channel._cfg["access_token"] == "good"

    def test_logout(self, channel):
        _configure(channel)
        channel.logout()
        assert not channel.configured and channel.status() == ChannelStatus.DISCONNECTED


class TestSending:
    def test_send_text(self, channel, meta):
        _configure(channel)
        assert channel.send_and_wait(f"{OWNER}@s.whatsapp.net", "hola")["ok"]
        assert meta.sent[-1]["to"] == OWNER and meta.sent[-1]["text"]["body"] == "hola"

    def test_outside_window_without_template(self, channel, meta):
        _configure(channel)
        meta.fail_window = True
        res = channel.send_and_wait("34611@s.whatsapp.net", "hola")
        assert not res["ok"] and "24 h" in res["error"]

    def test_outside_window_falls_back_to_template(self, channel, meta):
        _configure(channel, template_name="jarvis_aviso")
        meta.fail_window = True
        assert channel.send_and_wait("34611@s.whatsapp.net", "Recordatorio")["ok"]
        tpl = meta.sent[-1]["template"]
        assert tpl["name"] == "jarvis_aviso"
        assert tpl["components"][0]["parameters"][0]["text"] == "Recordatorio"


class TestWebhook:
    def test_signature(self, channel):
        _configure(channel)
        body = b'{"x":1}'
        assert channel.verify_signature(body, _sign(body))
        assert not channel.verify_signature(body, "sha256=bad")

    def test_owner_message_goes_to_agent(self, channel):
        _configure(channel)
        seen = []
        channel.on_message(seen.append)
        channel.handle_webhook(_webhook(OWNER, "¿qué tengo hoy?"))
        assert seen and seen[0].content == "¿qué tengo hoy?"
        assert seen[0].conversation_id == f"{OWNER}@s.whatsapp.net"

    def test_owner_confirms_pending_send(self, channel, meta):
        _configure(channel)
        channel.propose_send("34611@s.whatsapp.net", "Laura", "llego tarde")
        channel.handle_webhook(_webhook(OWNER, "sí"))
        bodies = [m["text"]["body"] for m in meta.sent if m["type"] == "text"]
        assert "llego tarde" in bodies
        assert any(b.startswith("✅ Enviado a Laura") for b in bodies)

    def test_third_party_goes_to_inbox_and_owner(self, channel, meta):
        _configure(channel)
        channel.handle_webhook(
            _webhook("34677000111", "Hola, ¿tenéis cita?", name="Clínica")
        )
        rows = channel.store.recent_messages(limit=5)
        assert rows and rows[0]["text"] == "Hola, ¿tenéis cita?"
        notice = meta.sent[-1]
        assert notice["to"] == OWNER and "Clínica" in notice["text"]["body"]

    def test_other_phone_number_ignored(self, channel, meta):
        _configure(channel)
        payload = _webhook(OWNER, "hola")
        payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = (
            "999"
        )
        assert channel.handle_webhook(payload) == 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client(channel):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from openjarvis.server.app import create_app

    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["m"]
    return TestClient(create_app(engine, "m", channel_bridge=channel, api_key="k"))


AUTH = {"Authorization": "Bearer k"}


class TestRoutes:
    def test_status_reports_meta(self, client):
        data = client.get("/api/whatsapp/status", headers=AUTH).json()
        assert data["enabled"] and data["provider"] == "meta" and not data["configured"]

    def test_config_and_test_message(self, client, meta):
        body = {
            "phone_number_id": "555",
            "access_token": "good",
            "owner_number": OWNER,
            "app_secret": SECRET,
        }
        r = client.post("/api/whatsapp/meta/config", json=body, headers=AUTH)
        assert r.status_code == 200 and r.json()["connected"]
        assert client.post("/api/whatsapp/meta/test", headers=AUTH).status_code == 200
        assert meta.sent[-1]["to"] == OWNER

    def test_config_rejected(self, client):
        body = {"phone_number_id": "555", "access_token": "bad", "owner_number": OWNER}
        assert (
            client.post(
                "/api/whatsapp/meta/config", json=body, headers=AUTH
            ).status_code
            == 400
        )

    def test_webhook_verify(self, client, channel):
        q = {
            "hub.mode": "subscribe",
            "hub.verify_token": channel.verify_token,
            "hub.challenge": "42",
        }
        assert client.get("/meta/whatsapp/webhook", params=q).text == "42"
        q["hub.verify_token"] = "nope"
        assert client.get("/meta/whatsapp/webhook", params=q).status_code == 403

    def test_webhook_requires_signature(self, client, channel):
        _configure(channel)
        body = json.dumps(_webhook("34677000111", "hola")).encode()
        assert client.post("/meta/whatsapp/webhook", content=body).status_code == 403
        r = client.post(
            "/meta/whatsapp/webhook",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body)},
        )
        assert r.status_code == 200
        assert channel.store.recent_messages(limit=1)


def test_registered():
    from openjarvis.core.registry import ChannelRegistry

    with patch.dict("os.environ", {}):
        import importlib

        import openjarvis.channels.whatsapp_cloud as mod

        importlib.reload(mod)
        assert ChannelRegistry.contains("whatsapp_cloud")
