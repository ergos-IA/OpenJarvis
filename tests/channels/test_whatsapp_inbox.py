"""Tests for the WhatsApp inbox, notifications, contacts and confirmed sends."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.channels import whatsapp_baileys as wb
from openjarvis.channels._stubs import ChannelMessage, ChannelStatus
from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
from openjarvis.channels.whatsapp_store import WhatsAppStore


@pytest.fixture
def store(tmp_path):
    s = WhatsAppStore(tmp_path / "wa.db")
    yield s
    s.close()


def _connected(store, **kwargs):
    """Channel with a fake bridge process, connected as +34600000000."""
    ch = WhatsAppBaileysChannel(store=store, **kwargs)
    ch._process = MagicMock()
    ch._handle_bridge_event(
        {"type": "status", "status": "connected", "me": "34600000000"}
    )
    return ch


def _commands(ch):
    """Commands the channel wrote to the bridge's stdin."""
    return [json.loads(c[0][0]) for c in ch._process.stdin.write.call_args_list]


def _inbox(**overrides):
    event = {
        "type": "inbox",
        "chat_jid": "34611111111@s.whatsapp.net",
        "sender_jid": "34611111111@s.whatsapp.net",
        "phone": "34611111111",
        "push_name": "Pepito",
        "text": "hola, ¿quedamos?",
        "message_id": "M1",
        "ts": time.time(),
        "is_group": False,
        "from_me": False,
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_contact_name_only_for_address_book(self, store):
        store.upsert_contact("1@s.whatsapp.net", phone="1", push_name="Push")
        assert store.contact_name("1@s.whatsapp.net") == ""
        store.upsert_contact("1@s.whatsapp.net", name="Pepe")
        assert store.contact_name(phone="1") == "Pepe"

    def test_upsert_keeps_existing_fields(self, store):
        store.upsert_contact("1@s.whatsapp.net", phone="1", name="Pepe")
        store.upsert_contact("1@s.whatsapp.net", push_name="P")
        [c] = store.search_contacts("Pepe")
        assert (c["name"], c["phone"], c["push_name"]) == ("Pepe", "1", "P")

    def test_duplicate_message_ignored(self, store):
        msg = {"message_id": "A", "chat_jid": "c", "text": "x", "ts": 1.0}
        assert store.add_message(msg) is True
        assert store.add_message(msg) is False

    def test_unread_then_read(self, store):
        store.add_message({"message_id": "A", "chat_jid": "c", "text": "x"})
        assert len(store.recent_messages(unread_only=True)) == 1
        assert store.recent_messages(unread_only=True) == []

    def test_filters(self, store):
        now = time.time()
        store.add_message(
            {
                "message_id": "A",
                "chat_jid": "a",
                "sender_name": "Ana",
                "text": "1",
                "ts": now,
            }
        )
        store.add_message(
            {
                "message_id": "B",
                "chat_jid": "g",
                "sender_name": "Bea",
                "text": "2",
                "ts": now - 10,
                "is_group": True,
            }
        )
        store.add_message(
            {
                "message_id": "C",
                "chat_jid": "a",
                "sender_name": "Ana",
                "text": "old",
                "ts": now - 7200,
            }
        )
        names = lambda rows: [r["message_id"] for r in rows]  # noqa: E731
        assert names(store.recent_messages(query="ana", mark_read=False)) == ["A", "C"]
        assert names(store.recent_messages(include_groups=False, mark_read=False)) == [
            "A",
            "C",
        ]
        assert names(store.recent_messages(since_hours=1, mark_read=False)) == [
            "A",
            "B",
        ]


# ---------------------------------------------------------------------------
# Inbox + notifications
# ---------------------------------------------------------------------------


class TestInbox:
    def test_stores_and_notifies_unknown_sender(self, store):
        ch = _connected(store, notify="all")
        ch._handle_bridge_event(_inbox())
        [row] = store.recent_messages(mark_read=False)
        assert row["sender_name"] == "Pepito"
        [cmd] = _commands(ch)
        assert cmd["jid"] == "34600000000@s.whatsapp.net"
        assert "no está en tus contactos" in cmd["text"]
        assert "hola, ¿quedamos?" in cmd["text"]

    def test_contact_name_used(self, store):
        store.upsert_contact("34611111111@s.whatsapp.net", name="Pepe Pérez")
        ch = _connected(store, notify="all")
        ch._handle_bridge_event(_inbox())
        [cmd] = _commands(ch)
        assert cmd["text"].startswith("📩 *Pepe Pérez*:")

    def test_contacts_mode_skips_strangers(self, store):
        ch = _connected(store, notify="contacts")
        ch._handle_bridge_event(_inbox())
        assert _commands(ch) == []
        assert len(store.recent_messages(mark_read=False)) == 1

    @pytest.mark.parametrize(
        "overrides",
        [{"is_group": True}, {"from_me": True}],
    )
    def test_groups_and_own_messages_not_notified(self, store, overrides):
        ch = _connected(store)
        ch._handle_bridge_event(_inbox(**overrides))
        assert _commands(ch) == []

    def test_notify_none(self, store):
        ch = _connected(store, notify="none")
        ch._handle_bridge_event(_inbox())
        assert _commands(ch) == []

    def test_duplicate_not_notified_twice(self, store):
        ch = _connected(store, notify="all")
        ch._handle_bridge_event(_inbox())
        ch._handle_bridge_event(_inbox())
        assert len(_commands(ch)) == 1

    def test_own_number_mode_never_notifies(self, store):
        ch = _connected(store, assistant_has_own_number=True)
        ch._handle_bridge_event(_inbox())
        assert _commands(ch) == []

    def test_contact_event(self, store):
        ch = _connected(store)
        ch._handle_bridge_event(
            {
                "type": "contact",
                "jid": "1@s.whatsapp.net",
                "phone": "1",
                "name": "Luis",
                "push_name": "",
            }
        )
        assert store.contact_name(phone="1") == "Luis"


# ---------------------------------------------------------------------------
# Sending with confirmation
# ---------------------------------------------------------------------------


def _auto_ack(ch, ok=True, error=""):
    """Make the fake bridge answer every request_id send with send_result."""

    def write(line):
        cmd = json.loads(line)
        if cmd.get("request_id"):
            threading.Timer(
                0.01,
                ch._handle_bridge_event,
                args=(
                    {
                        "type": "send_result",
                        "request_id": cmd["request_id"],
                        "ok": ok,
                        "jid": cmd["jid"],
                        "error": error,
                    },
                ),
            ).start()

    ch._process.stdin.write.side_effect = write


def _own_msg(text):
    return ChannelMessage(
        channel="whatsapp_baileys",
        sender="34600000000@s.whatsapp.net",
        content=text,
        message_id="X",
        conversation_id="34600000000@s.whatsapp.net",
    )


class TestConfirmedSend:
    def test_draft_then_si_sends(self, store):
        ch = _connected(store)
        _auto_ack(ch)
        handler = MagicMock()
        ch.on_message(handler)

        res = ch.propose_send("34611111111@s.whatsapp.net", "Pepe", "llego tarde")
        assert res["pending"] is True
        assert _commands(ch) == []  # nothing sent yet

        ch._dispatch_message(_own_msg("¡Sí!"))
        handler.assert_not_called()
        cmds = _commands(ch)
        assert cmds[0]["jid"] == "34611111111@s.whatsapp.net"
        assert cmds[0]["text"] == "llego tarde"
        assert cmds[-1]["text"] == "✅ Enviado a Pepe."
        assert ch._pending is None

    def test_no_cancels(self, store):
        ch = _connected(store)
        ch.propose_send("34611111111@s.whatsapp.net", "Pepe", "x")
        ch._dispatch_message(_own_msg("no"))
        [cmd] = _commands(ch)
        assert cmd["text"].startswith("❌")

    def test_other_text_goes_to_agent_and_keeps_draft(self, store):
        ch = _connected(store)
        handler = MagicMock()
        ch.on_message(handler)
        ch.propose_send("34611111111@s.whatsapp.net", "Pepe", "x")
        ch._dispatch_message(_own_msg("mejor cámbialo a las 9"))
        handler.assert_called_once()
        assert ch._pending is not None

    def test_expired_draft_not_sent(self, store):
        ch = _connected(store)
        handler = MagicMock()
        ch.on_message(handler)
        ch.propose_send("34611111111@s.whatsapp.net", "Pepe", "x")
        ch._pending["expires"] = time.time() - 1
        ch._dispatch_message(_own_msg("sí"))
        handler.assert_called_once()
        assert _commands(ch) == []

    def test_send_failure_reported(self, store):
        ch = _connected(store)
        _auto_ack(ch, ok=False, error="Ese número no tiene WhatsApp")
        ch.propose_send("34999999999@s.whatsapp.net", "+34999999999", "x")
        ch._dispatch_message(_own_msg("si"))
        assert "no tiene WhatsApp" in _commands(ch)[-1]["text"]

    def test_without_confirmation_sends_directly(self, store):
        ch = _connected(store, confirm_sends=False)
        _auto_ack(ch)
        res = ch.propose_send("34611111111@s.whatsapp.net", "Pepe", "hola")
        assert res["ok"] is True and res["pending"] is False

    def test_send_and_wait_times_out(self, store):
        ch = _connected(store)
        res = ch.send_and_wait("1@s.whatsapp.net", "x", timeout=0.05)
        assert res["ok"] is False

    def test_send_and_wait_requires_connection(self, store):
        ch = WhatsAppBaileysChannel(store=store)
        assert ch.send_and_wait("1@s.whatsapp.net", "x")["ok"] is False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@pytest.fixture
def active(store):
    ch = _connected(store)
    with patch.object(wb, "_active_channel", ch):
        yield ch


class TestTools:
    def test_tools_registered(self):
        import importlib

        import openjarvis.tools.whatsapp_tools as wt
        from openjarvis.core.registry import ToolRegistry

        # The test suite clears registries between tests; re-run the module's
        # registration decorators.
        importlib.reload(wt)

        for name in (
            "whatsapp_read_messages",
            "whatsapp_find_contact",
            "whatsapp_send_message",
        ):
            assert ToolRegistry.contains(name)

    def test_not_connected(self):
        from openjarvis.tools.whatsapp_tools import WhatsAppReadMessagesTool

        with patch.object(wb, "_active_channel", None):
            assert WhatsAppReadMessagesTool().execute().success is False

    def test_read_messages(self, active, store):
        from openjarvis.tools.whatsapp_tools import WhatsAppReadMessagesTool

        active._handle_bridge_event(_inbox())
        res = WhatsAppReadMessagesTool().execute(contact="pepito")
        assert res.success
        assert "Pepito (+34611111111): hola, ¿quedamos?" in res.content

    def test_find_contact(self, active, store):
        from openjarvis.tools.whatsapp_tools import WhatsAppFindContactTool

        store.upsert_contact(
            "34611111111@s.whatsapp.net", phone="34611111111", name="Pepe Pérez"
        )
        res = WhatsAppFindContactTool().execute(query="pepe")
        assert "Pepe Pérez (+34611111111)" in res.content

    def test_send_by_name_creates_draft(self, active, store):
        from openjarvis.tools.whatsapp_tools import WhatsAppSendMessageTool

        store.upsert_contact("x@lid", phone="34611111111", name="Pepe Pérez")
        res = WhatsAppSendMessageTool().execute(to="Pepe", text="llego tarde")
        assert res.success
        assert "TODAVÍA NO ESTÁ ENVIADO" in res.content
        assert active._pending["jid"] == "34611111111@s.whatsapp.net"
        assert _commands(active) == []

    def test_send_to_local_number_adds_country_code(self, active):
        from openjarvis.tools.whatsapp_tools import WhatsAppSendMessageTool

        WhatsAppSendMessageTool().execute(to="600 111 222", text="hola")
        assert active._pending["jid"] == "34600111222@s.whatsapp.net"

    def test_ambiguous_contact(self, active, store):
        from openjarvis.tools.whatsapp_tools import WhatsAppSendMessageTool

        store.upsert_contact("1@s.whatsapp.net", phone="1", name="Pepe A")
        store.upsert_contact("2@s.whatsapp.net", phone="2", name="Pepe B")
        res = WhatsAppSendMessageTool().execute(to="Pepe", text="x")
        assert res.success is False
        assert "varios contactos" in res.content
        assert active._pending is None

    def test_unknown_contact(self, active):
        from openjarvis.tools.whatsapp_tools import WhatsAppSendMessageTool

        res = WhatsAppSendMessageTool().execute(to="Nadie", text="x")
        assert res.success is False


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestAsyncDispatch:
    def test_messages_run_on_worker_after_connect(self, store):
        ch = WhatsAppBaileysChannel(store=store)
        ch._executor = wb.ThreadPoolExecutor(max_workers=1)
        seen = []
        done = threading.Event()

        def handler(cm):
            seen.append(threading.current_thread().name)
            done.set()

        ch.on_message(handler)
        ch._handle_bridge_event(
            {
                "type": "message",
                "jid": "j",
                "sender": "s",
                "text": "t",
                "message_id": "m",
            }
        )
        assert done.wait(2)
        assert seen[0] != threading.current_thread().name
        ch._executor.shutdown()


def test_system_prompt_mentions_tools():
    prompt = WhatsAppBaileysChannel(assistant_name="Viernes").system_prompt
    assert "Viernes" in prompt
    assert "whatsapp_send_message" in prompt


def test_status_unchanged_by_inbox(store):
    ch = _connected(store)
    ch._handle_bridge_event(_inbox())
    assert ch.status() == ChannelStatus.CONNECTED


class TestImportantMode:
    """Default mode: only VIPs, urgent words, insistence or AI-judged."""

    def _ch(self, store, answer="NO"):
        from types import SimpleNamespace

        ch = _connected(store)
        calls = []

        def generate(messages, **kw):
            calls.append(messages)
            return {"content": answer}

        ch._system = SimpleNamespace(
            engine=SimpleNamespace(generate=generate), model="m"
        )
        return ch, calls

    def test_chit_chat_is_not_forwarded_but_stored(self, store):
        ch, calls = self._ch(store, "NO")
        ch._handle_bridge_event(_inbox(text="jajaja qué bueno el vídeo de ayer"))
        assert _commands(ch) == [] and len(calls) == 1
        assert len(store.recent_messages(mark_read=False)) == 1

    def test_urgent_words_skip_the_ai(self, store):
        ch, calls = self._ch(store)
        ch._handle_bridge_event(_inbox(text="Llámame cuando puedas, es URGENTE"))
        [cmd] = _commands(ch)
        assert "🔴 urgente" in cmd["text"] and calls == []

    def test_ai_judged_important(self, store):
        ch, _ = self._ch(store, "SÍ")
        ch._handle_bridge_event(
            _inbox(text="Mañana a las 9 cambiamos la reunión al despacho")
        )
        [cmd] = _commands(ch)
        assert "❗ importante" in cmd["text"]

    def test_vip_always(self, store):
        store.upsert_contact("34611111111@s.whatsapp.net", name="Ana López")
        ch, calls = self._ch(store)
        ch.notify_policy.add_vip("Ana")
        ch._handle_bridge_event(_inbox(text="hola"))
        [cmd] = _commands(ch)
        assert cmd["text"].startswith("📩 *Ana López* ⭐") and calls == []

    def test_insisting_sender(self, store):
        ch, _ = self._ch(store)
        for i in range(3):
            ch._handle_bridge_event(_inbox(text=f"oye {i}", message_id=f"x{i}"))
        cmds = _commands(ch)
        assert len(cmds) == 1 and "🔁 insiste" in cmds[0]["text"]

    def test_mode_can_change_at_runtime(self, store):
        ch, calls = self._ch(store)
        ch.notify_policy.update(mode="none")
        ch._handle_bridge_event(_inbox(text="URGENTE"))
        assert _commands(ch) == [] and calls == []
