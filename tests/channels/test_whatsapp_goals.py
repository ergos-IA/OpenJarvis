"""Following the user's goal: natural send orders, real draft state, no silence.

Cases taken from a real chat: «Envíaselo a Oscar» (voice), «Si a Oscar», a
"sí" the next morning, drafts that had expired but were still mentioned.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.channels import whatsapp_baileys as wb
from openjarvis.channels._stubs import ChannelMessage
from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
from openjarvis.channels.whatsapp_store import WhatsAppStore
from openjarvis.personal.assistant_prefs import AssistantPrefs, set_prefs

OSCAR = "34600453451@s.whatsapp.net"
ME = "34600000000@s.whatsapp.net"


@pytest.fixture
def store(tmp_path):
    s = WhatsAppStore(tmp_path / "wa.db")
    yield s
    s.close()


@pytest.fixture
def ch(store):
    set_prefs(AssistantPrefs())
    channel = WhatsAppBaileysChannel(store=store)
    channel._process = MagicMock()
    channel._handle_bridge_event(
        {"type": "status", "status": "connected", "me": "34600000000"}
    )

    def write(line):
        cmd = json.loads(line)
        if cmd.get("request_id"):
            threading.Timer(
                0.01,
                channel._handle_bridge_event,
                args=(
                    {
                        "type": "send_result",
                        "request_id": cmd["request_id"],
                        "ok": True,
                        "jid": cmd["jid"],
                    },
                ),
            ).start()

    channel._process.stdin.write.side_effect = write
    with patch.object(wb, "_active_channel", channel):
        yield channel
    set_prefs(None)


def _sent(ch):
    return [json.loads(c[0][0]) for c in ch._process.stdin.write.call_args_list]


def _msg(text):
    return ChannelMessage(
        channel="whatsapp_baileys",
        sender=ME,
        content=text,
        message_id="X",
        conversation_id=ME,
    )


def _draft(ch):
    ch.propose_send(OSCAR, "Oscar Peña (CTO)", "Tenemos reunión urgente.")


NAMES = {"oscar", "pena", "cto"}


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Envíaselo a Oscar.", "confirm"),
        ("Si a Oscar", "confirm"),
        ("sí, envíaselo a Óscar Peña", "confirm"),
        ("mándalo ya", "confirm"),
        ("Sí a las 9 cámbialo", ""),
        ("mándale a Pepe que llego tarde", ""),
        ("no se lo mandes a Oscar", ""),
    ],
)
def test_send_orders_with_recipient(text, kind):
    assert wb._classify_reply(wb._normalize_reply(text), NAMES) == kind


def test_voice_order_sends_draft(ch):
    handler = MagicMock()
    ch.on_message(handler)
    _draft(ch)
    ch._dispatch_message(_msg(f"{wb._VOICE_PREFIX} Envíaselo a Oscar."))
    handler.assert_not_called()
    sent = _sent(ch)
    assert sent[0]["jid"] == OSCAR and sent[0]["text"] == "Tenemos reunión urgente."
    assert sent[-1]["text"] == "✅ Enviado a Oscar Peña (CTO)."


def test_bare_si_after_other_topics_needs_the_name(ch):
    handler = MagicMock()
    ch.on_message(handler)
    _draft(ch)
    ch._dispatch_message(_msg("¿me pones un recordatorio a las dos?"))
    ch._dispatch_message(_msg("sí"))  # may answer «¿te pongo otro?»
    assert handler.call_count == 2 and ch._pending is not None
    ch._dispatch_message(_msg("Sí a Oscar"))
    assert _sent(ch)[-1]["text"] == "✅ Enviado a Oscar Peña (CTO)."


def test_confirmation_is_remembered(ch):
    from openjarvis.personal.memory import get_memory

    _draft(ch)
    ch._dispatch_message(_msg("sí"))
    texts = [m["text"] for m in get_memory().log_between(0, time.time() + 1)]
    assert texts[-1] == "✅ Enviado a Oscar Peña (CTO)."


def test_draft_lives_until_next_morning(ch):
    _draft(ch)
    ch._pending["expires"] -= 20 * 3600
    assert "Oscar Peña" in ch.pending_summary()


def test_prompt_states_real_drafts(ch):
    assert "NINGUNO" in ch.system_prompt
    _draft(ch)
    prompt = ch.system_prompt
    assert "Tenemos reunión urgente." in prompt and "whatsapp_pending_draft" in prompt
    ch._pending["expires"] = time.time() - 1
    assert "NINGUNO" in ch.system_prompt


def test_prompt_teaches_goals_and_voice(ch):
    prompt = ch.system_prompt
    assert "Entiende el objetivo" in prompt
    assert "O sea" in prompt
    assert "Nunca le pidas que la repita por escrito" in prompt


class TestPendingTool:
    def _tool(self):
        from openjarvis.tools.whatsapp_tools import WhatsAppPendingDraftTool

        return WhatsAppPendingDraftTool()

    def test_send_on_users_order(self, ch):
        _draft(ch)
        wb._current.text = "[Nota de voz del usuario, transcrita] vale, mándaselo"
        try:
            res = self._tool().execute(action="send")
        finally:
            wb._current.text = ""
        assert res.success and res.content.startswith("✅")
        assert _sent(ch)[0]["jid"] == OSCAR

    def test_no_send_without_users_order(self, ch):
        _draft(ch)
        wb._current.text = "léeme lo que me ha escrito Pepe"
        try:
            res = self._tool().execute(action="send")
        finally:
            wb._current.text = ""
        assert res.success is False and ch._pending is not None
        assert _sent(ch) == []

    def test_nothing_pending(self, ch):
        wb._current.text = "sí, envíalo"
        try:
            res = self._tool().execute(action="send")
        finally:
            wb._current.text = ""
        assert res.success is False and "No hay ningún borrador" in res.content

    def test_cancel(self, ch):
        _draft(ch)
        res = self._tool().execute(action="cancel")
        assert res.content.startswith("❌") and ch._pending is None


class TestNumbersTheUserGave:
    def test_number_in_message_is_sure(self, ch):
        from openjarvis.tools.whatsapp_tools import WhatsAppSendMessageTool

        wb._current.text = "+34 600 45 34 51 y dile que tenemos reunión"
        try:
            res = WhatsAppSendMessageTool().execute(
                to="+34 600 45 34 51", text="Tenemos reunión urgente."
            )
        finally:
            wb._current.text = ""
        assert "enviado" in res.content and ch._pending is None

    def test_number_remembered_by_user_is_sure(self, ch):
        from openjarvis.personal.memory import get_memory
        from openjarvis.tools.whatsapp_tools import WhatsAppSendMessageTool

        get_memory().add_fact(
            "Oscar Peña (CTO) tiene el número +34 600 45 34 51", source="usuario"
        )
        res = WhatsAppSendMessageTool().execute(to="+34600453451", text="hola")
        assert "enviado" in res.content

    def test_unknown_number_still_drafts(self, ch):
        from openjarvis.tools.whatsapp_tools import WhatsAppSendMessageTool

        res = WhatsAppSendMessageTool().execute(to="+34 611 22 33 44", text="hola")
        assert "TODAVÍA NO ESTÁ ENVIADO" in res.content


def test_empty_answer_gets_a_reply():
    """wire_channel never leaves the user without an answer."""
    from openjarvis.system.core import JarvisSystem

    system = MagicMock(spec=JarvisSystem)
    system.agent_name = "orchestrator"
    system.ask.return_value = {"content": ""}
    system.session_store = MagicMock()
    bridge = MagicMock()
    bridge.system_prompt = "x"
    bridge.empty_reply = wb.EMPTY_REPLY
    JarvisSystem.wire_channel(system, bridge)
    handler = bridge.on_message.call_args[0][0]
    handler(_msg("mírame el correo"))
    assert bridge.send.call_args[0][1] == wb.EMPTY_REPLY
