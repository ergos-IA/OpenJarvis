"""Voice notes in the assistant chat: transcription, forwarded audios."""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
from openjarvis.personal import voice

JID = "34600000000@s.whatsapp.net"


@pytest.fixture
def channel(tmp_path):
    ch = WhatsAppBaileysChannel(auth_dir=str(tmp_path / "auth"))
    sent, handled = [], []
    ch.send = lambda jid, text, **kw: sent.append((jid, text)) or True
    ch.on_message(lambda cm: handled.append(cm))
    ch._executor = None
    return ch, sent, handled


def _event(**kw):
    ev = {
        "type": "audio",
        "jid": JID,
        "sender": JID,
        "message_id": "m1",
        "seconds": 12,
        "mimetype": "audio/ogg; codecs=opus",
        "ptt": True,
        "forwarded": False,
        "data": base64.b64encode(b"OggS-fake").decode(),
    }
    ev.update(kw)
    return ev


def test_own_voice_note_is_a_request(channel):
    ch, sent, handled = channel
    with patch.object(
        voice, "transcribe", return_value="Recuérdame llamar a Ana a las 5"
    ) as tr:
        ch._handle_bridge_event(_event())
    assert tr.call_args.args[0] == b"OggS-fake"
    assert handled and handled[0].content.endswith("Recuérdame llamar a Ana a las 5")
    assert "Nota de voz" in handled[0].content and not sent


def test_forwarded_audio_is_transcribed_back(channel):
    ch, sent, handled = channel
    with patch.object(
        voice, "transcribe", return_value="Hola, soy Marta, te llamo mañana"
    ):
        ch._handle_bridge_event(_event(forwarded=True, seconds=75))
    assert not handled
    assert "Escuchando el audio (1:15)" in sent[0][1]
    assert "Transcripción del audio reenviado" in sent[1][1]
    assert "soy Marta" in sent[1][1]


def test_long_forwarded_audio_gets_a_summary(channel):
    ch, sent, _ = channel
    ch.task_runner = lambda prompt: "- Marta llama mañana"
    with patch.object(voice, "transcribe", return_value="bla " * 300):
        ch._handle_bridge_event(_event(forwarded=True))
    assert "*Resumen:*" in sent[-1][1] and "Marta llama" in sent[-1][1]


def test_voice_yes_confirms_pending_send(channel):
    ch, sent, handled = channel
    with (
        patch.object(ch, "_take_pending_reply", return_value="✅ Enviado") as pending,
        patch.object(voice, "transcribe", return_value="Sí"),
    ):
        ch._handle_bridge_event(_event())
    pending.assert_called_with("Sí")
    assert sent == [(JID, "✅ Enviado")] and not handled


@pytest.mark.parametrize(
    "event,expected",
    [
        (_event(error="too_long", data=None), "demasiado largo"),
        (_event(error="download_failed", data=None), "No he podido descargar"),
    ],
)
def test_bridge_errors(channel, event, expected):
    ch, sent, _ = channel
    ch._handle_bridge_event(event)
    assert expected in sent[0][1]


def test_transcription_failure_asks_to_type(channel):
    ch, sent, handled = channel
    with patch.object(
        voice,
        "transcribe",
        side_effect=voice.VoiceError("No he podido entender el audio."),
    ):
        ch._handle_bridge_event(_event())
    assert "¿Me lo escribes?" in sent[0][1] and not handled


def test_backend_choice_and_format(monkeypatch):
    assert voice._format("audio/ogg; codecs=opus") == "ogg"
    assert voice._format("audio/mp4") == "m4a"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    from openjarvis.speech.openai_whisper import OpenAIWhisperBackend

    assert isinstance(voice._get_backend(), OpenAIWhisperBackend)


def test_transcribe_uses_spanish_and_strips(monkeypatch):
    class Fake:
        def transcribe(self, audio, *, format, language):
            assert format == "ogg" and language == "es"
            return type("R", (), {"text": "  hola  "})()

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(voice, "_backend", Fake())
    monkeypatch.setattr(voice, "_schedule_unload", lambda: None)
    assert voice.transcribe(b"x", "audio/ogg") == "hola"
    with pytest.raises(voice.VoiceError):
        voice.transcribe(b"")
