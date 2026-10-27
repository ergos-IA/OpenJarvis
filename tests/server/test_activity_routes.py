"""Portal "Actividad": assistant chat, received chats and the AI summary."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.channels.whatsapp_store import WhatsAppStore
from openjarvis.personal.memory import LongMemory, set_memory
from openjarvis.server import activity_routes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = WhatsAppStore(tmp_path / "wa.db")
    store.upsert_contact("34611@s.whatsapp.net", phone="34611", name="Óscar Peña")
    now = time.time()
    for i, text in enumerate(["¿Mañana a las 9?", "Confírmame porfa"]):
        store.add_message(
            {
                "message_id": f"m{i}",
                "chat_jid": "34611@s.whatsapp.net",
                "sender_jid": "34611@s.whatsapp.net",
                "sender_name": "Oscar",
                "phone": "34611",
                "text": text,
                "ts": now - 60 + i,
            }
        )
    memory = LongMemory(tmp_path / "memory.db")
    memory.record("user", "escribe a oscar", ts=now - 30)
    memory.record("assistant", "✅ Enviado", ts=now - 29)
    set_memory(memory)

    calls = []

    def generate(messages, **kw):
        calls.append(messages)
        return {"content": "*Lo importante*: Óscar pide confirmar mañana a las 9."}

    system = SimpleNamespace(
        engine=SimpleNamespace(generate=generate),
        model="m",
    )
    ch = SimpleNamespace(store=store, _system=system)
    monkeypatch.setattr(activity_routes, "_channel", lambda request: ch)
    monkeypatch.setattr(
        "openjarvis.channels.whatsapp_baileys._credit_block", lambda: None
    )
    app = FastAPI()
    app.include_router(activity_routes.router)
    yield TestClient(app), store, calls
    set_memory(None)
    store.close()


def test_assistant_chat(setup):
    client, _, _ = setup
    msgs = client.get("/api/activity/assistant").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "escribe a oscar"


def test_chat_list_uses_contact_name_and_keeps_unread(setup):
    client, store, _ = setup
    [chat] = client.get("/api/activity/chats").json()["chats"]
    assert chat["name"] == "Óscar Peña" and chat["unread"] == 2 and chat["count"] == 2
    assert chat["last_text"] == "Confírmame porfa"
    thread = client.get("/api/activity/chat", params={"jid": chat["jid"]}).json()
    assert [m["text"] for m in thread["messages"]] == [
        "¿Mañana a las 9?",
        "Confírmame porfa",
    ]
    # Viewing in the portal does not mark them as read for Jarvis.
    assert len(store.recent_messages(unread_only=True, mark_read=False)) == 2


def test_summary(setup):
    client, _, calls = setup
    r = client.post("/api/activity/summary", json={"hours": 24}).json()
    assert "Óscar" in r["summary"]
    prompt = calls[0][1].content
    assert "Confírmame porfa" in prompt and "escribe a oscar" in prompt
