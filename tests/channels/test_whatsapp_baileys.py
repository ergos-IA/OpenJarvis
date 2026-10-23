"""Tests for the WhatsAppBaileysChannel adapter."""

from __future__ import annotations

import json
import threading
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.channels._stubs import ChannelMessage, ChannelStatus
from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.registry import ChannelRegistry


@pytest.fixture(autouse=True)
def _register_whatsapp_baileys():
    """Re-register after any registry clear."""
    if not ChannelRegistry.contains("whatsapp_baileys"):
        ChannelRegistry.register_value("whatsapp_baileys", WhatsAppBaileysChannel)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registry_key(self):
        assert ChannelRegistry.contains("whatsapp_baileys")

    def test_channel_id(self):
        ch = WhatsAppBaileysChannel()
        assert ch.channel_id == "whatsapp_baileys"


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


class TestInit:
    def test_defaults(self):
        ch = WhatsAppBaileysChannel()
        assert ch._auth_dir == ""
        assert ch._assistant_name == "Jarvis"
        assert ch._assistant_has_own_number is False
        assert ch._status == ChannelStatus.DISCONNECTED
        assert ch._process is None
        assert ch._handlers == []

    def test_custom_params(self):
        ch = WhatsAppBaileysChannel(
            auth_dir="/tmp/auth",
            assistant_name="Bot",
            assistant_has_own_number=True,
        )
        assert ch._auth_dir == "/tmp/auth"
        assert ch._assistant_name == "Bot"
        assert ch._assistant_has_own_number is True


# ---------------------------------------------------------------------------
# _ensure_bridge
# ---------------------------------------------------------------------------


class TestEnsureBridge:
    def test_raises_when_node_not_found(self):
        ch = WhatsAppBaileysChannel()
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="Node.js is required"):
                ch._ensure_bridge()


# ---------------------------------------------------------------------------
# Connect / Disconnect lifecycle
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    def test_connect_spawns_subprocess(self, tmp_path):
        ch = WhatsAppBaileysChannel()
        ch._runtime_dir = tmp_path

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = StringIO("")
        mock_proc.stderr = MagicMock()
        mock_proc.pid = 12345

        bridge_js = tmp_path / "dist" / "bridge.js"
        bridge_js.parent.mkdir(parents=True, exist_ok=True)
        bridge_js.write_text("// bridge")

        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run"),  # npm install
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            # Pretend node_modules already exists to skip npm install.
            (tmp_path / "node_modules").mkdir()
            ch.connect()

            mock_popen.assert_called_once()
            call_args = mock_popen.call_args
            assert "node" in call_args[0][0][0]
            assert str(bridge_js) in call_args[0][0][1]

        # Cleanup.
        ch._stop_event.set()
        ch._process = None

    def test_connect_sets_error_when_node_missing(self):
        ch = WhatsAppBaileysChannel()
        with patch("shutil.which", return_value=None):
            ch.connect()
            assert ch.status() == ChannelStatus.ERROR

    def test_disconnect_terminates_process(self):
        ch = WhatsAppBaileysChannel()
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        ch._process = mock_proc
        ch._status = ChannelStatus.CONNECTED

        ch.disconnect()

        mock_proc.terminate.assert_called_once()
        assert ch.status() == ChannelStatus.DISCONNECTED
        assert ch._process is None

    def test_disconnect_when_not_connected(self):
        ch = WhatsAppBaileysChannel()
        ch.disconnect()
        assert ch.status() == ChannelStatus.DISCONNECTED


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    def test_send_writes_json_to_stdin(self):
        ch = WhatsAppBaileysChannel()
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        ch._process = mock_proc
        ch._status = ChannelStatus.CONNECTED

        result = ch.send("123456@s.whatsapp.net", "Hello!")
        assert result is True

        written = mock_proc.stdin.write.call_args[0][0]
        payload = json.loads(written.strip())
        assert payload["type"] == "send"
        assert payload["jid"] == "123456@s.whatsapp.net"
        assert payload["text"] == "Hello!"

    def test_send_fails_when_not_connected(self):
        ch = WhatsAppBaileysChannel()
        result = ch.send("123456@s.whatsapp.net", "Hello!")
        assert result is False

    def test_send_publishes_event(self):
        bus = EventBus(record_history=True)
        ch = WhatsAppBaileysChannel(bus=bus)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        ch._process = mock_proc
        ch._status = ChannelStatus.CONNECTED

        ch.send("123@s.whatsapp.net", "Hi!")
        event_types = [e.event_type for e in bus.history]
        assert EventType.CHANNEL_MESSAGE_SENT in event_types


# ---------------------------------------------------------------------------
# on_message
# ---------------------------------------------------------------------------


class TestOnMessage:
    def test_handler_registration(self):
        ch = WhatsAppBaileysChannel()
        handler = MagicMock()
        ch.on_message(handler)
        assert handler in ch._handlers


# ---------------------------------------------------------------------------
# list_channels / status
# ---------------------------------------------------------------------------


class TestListChannelsAndStatus:
    def test_list_channels(self):
        ch = WhatsAppBaileysChannel()
        assert ch.list_channels() == ["whatsapp_baileys"]

    def test_initial_status(self):
        ch = WhatsAppBaileysChannel()
        assert ch.status() == ChannelStatus.DISCONNECTED


# ---------------------------------------------------------------------------
# _reader_loop + _handle_bridge_event
# ---------------------------------------------------------------------------


class TestReaderLoop:
    def test_parses_message_event(self):
        ch = WhatsAppBaileysChannel()
        handler = MagicMock()
        ch.on_message(handler)

        event = {
            "type": "message",
            "jid": "123@s.whatsapp.net",
            "sender": "456@s.whatsapp.net",
            "text": "Hello from WhatsApp",
            "message_id": "msg-001",
        }
        ch._handle_bridge_event(event)

        handler.assert_called_once()
        msg: ChannelMessage = handler.call_args[0][0]
        assert msg.channel == "whatsapp_baileys"
        assert msg.sender == "456@s.whatsapp.net"
        assert msg.content == "Hello from WhatsApp"
        assert msg.message_id == "msg-001"
        assert msg.conversation_id == "123@s.whatsapp.net"

    def test_parses_status_connected(self):
        ch = WhatsAppBaileysChannel()
        ch._handle_bridge_event({"type": "status", "status": "connected"})
        assert ch.status() == ChannelStatus.CONNECTED

    def test_parses_status_disconnected(self):
        ch = WhatsAppBaileysChannel()
        ch._status = ChannelStatus.CONNECTED
        ch._handle_bridge_event({"type": "status", "status": "disconnected"})
        assert ch.status() == ChannelStatus.DISCONNECTED

    def test_parses_qr_event(self):
        ch = WhatsAppBaileysChannel()
        ch._handle_bridge_event({"type": "qr", "data": "qr-code-string"})
        assert ch._last_qr == "qr-code-string"

    def test_parses_fatal_error_event(self):
        ch = WhatsAppBaileysChannel()
        ch._handle_bridge_event({"type": "error", "message": "Fatal: boom"})
        assert ch.status() == ChannelStatus.ERROR

    def test_non_fatal_error_keeps_connection(self):
        ch = WhatsAppBaileysChannel()
        ch._handle_bridge_event({"type": "status", "status": "connected"})
        ch._handle_bridge_event({"type": "error", "message": "Send failed: x"})
        assert ch.status() == ChannelStatus.CONNECTED
        assert ch.pairing_info()["error"] == "Send failed: x"

    def test_message_event_publishes_to_bus(self):
        bus = EventBus(record_history=True)
        ch = WhatsAppBaileysChannel(bus=bus)

        ch._handle_bridge_event(
            {
                "type": "message",
                "jid": "123@s.whatsapp.net",
                "sender": "456@s.whatsapp.net",
                "text": "Bus test",
                "message_id": "msg-002",
            }
        )

        event_types = [e.event_type for e in bus.history]
        assert EventType.CHANNEL_MESSAGE_RECEIVED in event_types

    def test_reader_loop_processes_lines(self):
        ch = WhatsAppBaileysChannel()
        ch._stop_event = threading.Event()

        lines = [
            json.dumps({"type": "status", "status": "connected"}) + "\n",
            json.dumps(
                {
                    "type": "message",
                    "jid": "j",
                    "sender": "s",
                    "text": "t",
                    "message_id": "m",
                }
            )
            + "\n",
        ]

        mock_proc = MagicMock()
        mock_proc.stdout = lines
        ch._process = mock_proc

        ch._reader_loop()

        assert ch.status() == ChannelStatus.CONNECTED

    def test_reader_loop_skips_non_json(self):
        ch = WhatsAppBaileysChannel()
        ch._stop_event = threading.Event()

        lines = [
            "not json at all\n",
            json.dumps({"type": "status", "status": "connected"}) + "\n",
        ]

        mock_proc = MagicMock()
        mock_proc.stdout = lines
        ch._process = mock_proc

        ch._reader_loop()

        assert ch.status() == ChannelStatus.CONNECTED

    def test_handler_exception_does_not_crash(self):
        ch = WhatsAppBaileysChannel()
        bad_handler = MagicMock(side_effect=ValueError("boom"))
        ch.on_message(bad_handler)

        # Should not raise.
        ch._handle_bridge_event(
            {
                "type": "message",
                "jid": "j",
                "sender": "s",
                "text": "t",
                "message_id": "m",
            }
        )
        bad_handler.assert_called_once()


# ---------------------------------------------------------------------------
# Personal-assistant mode, allowlist and pre-built bridge
# ---------------------------------------------------------------------------


def _spawn_args(ch, tmp_path):
    """Run connect() with a fake bridge and return the spawned argv."""
    ch._runtime_dir = tmp_path
    bridge_js = tmp_path / "dist" / "bridge.js"
    bridge_js.parent.mkdir(parents=True, exist_ok=True)
    bridge_js.write_text("// bridge")
    (tmp_path / "node_modules").mkdir(exist_ok=True)

    mock_proc = MagicMock()
    mock_proc.stdout = StringIO("")
    mock_proc.stderr = StringIO("")
    with (
        patch("shutil.which", return_value="/usr/bin/node"),
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
    ):
        ch.connect()
        argv = mock_popen.call_args[0][0]
    ch._stop_event.set()
    ch._process = None
    return argv


class TestModes:
    def test_default_is_self_mode(self, tmp_path):
        argv = _spawn_args(WhatsAppBaileysChannel(), tmp_path)
        assert argv[argv.index("--mode") + 1] == "self"
        assert "--allowed" not in argv

    def test_own_number_passes_allowlist(self, tmp_path):
        ch = WhatsAppBaileysChannel(
            assistant_has_own_number=True,
            allowed_numbers="+34 600 111 222, 34600333444",
        )
        argv = _spawn_args(ch, tmp_path)
        assert argv[argv.index("--mode") + 1] == "own"
        assert argv[argv.index("--allowed") + 1] == "34600111222,34600333444"

    def test_allowlist_from_env(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "34600111222")
        ch = WhatsAppBaileysChannel(assistant_has_own_number=True)
        assert ch._allowed_numbers == ["34600111222"]

    def test_config_kwargs_forward_allowlist(self):
        from openjarvis.core.config import ChannelConfig
        from openjarvis.system._channel_kwargs import build_channel_kwargs

        cfg = ChannelConfig()
        cfg.whatsapp_baileys.assistant_has_own_number = True
        cfg.whatsapp_baileys.allowed_numbers = "34600111222"
        kw = build_channel_kwargs(cfg, "whatsapp_baileys")
        assert kw["allowed_numbers"] == "34600111222"
        assert kw["assistant_has_own_number"] is True


class TestPrebuiltBridge:
    def test_uses_prebuilt_dir_without_npm(self, tmp_path, monkeypatch):
        prebuilt = tmp_path / "prebuilt"
        (prebuilt / "dist").mkdir(parents=True)
        (prebuilt / "dist" / "bridge.js").write_text("// built")
        (prebuilt / "node_modules").mkdir()
        monkeypatch.setenv("OPENJARVIS_BAILEYS_BRIDGE_DIR", str(prebuilt))

        ch = WhatsAppBaileysChannel()
        ch._runtime_dir = tmp_path / "runtime"
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
        ):
            assert ch._ensure_bridge() == prebuilt / "dist" / "bridge.js"
            mock_run.assert_not_called()


class TestPairingState:
    def test_logged_out_clears_number(self):
        ch = WhatsAppBaileysChannel()
        ch._handle_bridge_event(
            {"type": "status", "status": "connected", "me": "34600111222"}
        )
        ch._handle_bridge_event({"type": "status", "status": "logged_out"})
        info = ch.pairing_info()
        assert info["connected"] is False
        assert info["me"] == ""

    def test_logout_without_process(self):
        assert WhatsAppBaileysChannel().logout() is False


class TestRestart:
    def test_respawns_after_unexpected_exit(self):
        ch = WhatsAppBaileysChannel()
        proc = MagicMock()
        proc.stdout = StringIO("")
        ch._process = proc
        ch._supervise = True
        with (
            patch("openjarvis.channels.whatsapp_baileys._RESTART_DELAY_S", 0),
            patch.object(ch, "connect") as mock_connect,
        ):
            ch._reader_loop()
        mock_connect.assert_called_once()
        assert ch._process is None

    def test_no_respawn_when_stopping(self):
        ch = WhatsAppBaileysChannel()
        proc = MagicMock()
        proc.stdout = StringIO("")
        ch._process = proc
        ch._stop_event.set()
        with patch.object(ch, "connect") as mock_connect:
            ch._reader_loop()
        mock_connect.assert_not_called()
