"""WhatsAppBaileysChannel -- bidirectional WhatsApp messaging via Baileys protocol.

Spawns a Node.js subprocess that runs the Baileys bridge (JSON-line protocol
on stdio).  The bridge handles QR-code authentication, message sending, and
incoming-message forwarding.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.channels._stubs import (
    BaseChannel,
    ChannelHandler,
    ChannelMessage,
    ChannelStatus,
)
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import ChannelRegistry

logger = logging.getLogger(__name__)

# Path to the bundled bridge shipped inside the package.
# In editable installs this lives next to this file; in wheel installs
# it is placed under _node_modules/ to avoid namespace package conflicts.
_BRIDGE_SRC = Path(__file__).resolve().parent / "whatsapp_baileys_bridge"
if not _BRIDGE_SRC.exists():
    _BRIDGE_SRC = (
        Path(__file__).resolve().parents[2]
        / "_node_modules"
        / "whatsapp_baileys_bridge"
    )

# Default runtime directory (npm install + auth state).
_DEFAULT_RUNTIME_DIR = get_config_dir() / "whatsapp_baileys_bridge"

# Optional pre-built bridge (``dist/`` + ``node_modules/``), e.g. baked into a
# container image, so no ``npm install`` runs at startup.
_PREBUILT_ENV = "OPENJARVIS_BAILEYS_BRIDGE_DIR"

# Seconds to wait before respawning a bridge process that exited unexpectedly.
_RESTART_DELAY_S = 5.0


def _parse_numbers(value: str | List[str] | None) -> List[str]:
    """Normalise a comma-separated string or list of phone numbers to digits."""
    if not value:
        return []
    items = value.split(",") if isinstance(value, str) else value
    out = []
    for item in items:
        num = "".join(ch for ch in str(item) if ch.isdigit())
        if num:
            out.append(num)
    return out


@ChannelRegistry.register("whatsapp_baileys")
class WhatsAppBaileysChannel(BaseChannel):
    """Bidirectional WhatsApp channel using the Baileys protocol.

    Communicates with a Node.js bridge subprocess over JSON-line stdio.

    Parameters
    ----------
    auth_dir:
        Directory for Baileys auth state persistence.  Defaults to
        ``~/.openjarvis/whatsapp_baileys_bridge/auth``.
    assistant_name:
        Display name used by the assistant in conversations.
    assistant_has_own_number:
        If ``False`` (default) the bridge is linked to the user's personal
        number and only the "Message yourself" chat talks to the assistant.
        If ``True`` the assistant has a dedicated number and answers only the
        numbers in *allowed_numbers*.
    allowed_numbers:
        Phone numbers (with country code) allowed to talk to a dedicated
        assistant number.  Falls back to ``WHATSAPP_ALLOWED_NUMBERS``.
    bus:
        Optional event bus for publishing channel events.
    """

    channel_id = "whatsapp_baileys"

    def __init__(
        self,
        *,
        auth_dir: str = "",
        assistant_name: str = "Jarvis",
        assistant_has_own_number: bool = False,
        allowed_numbers: str | List[str] = "",
        bus: Optional[EventBus] = None,
    ) -> None:
        self._auth_dir = auth_dir
        self._assistant_name = assistant_name
        self._assistant_has_own_number = assistant_has_own_number
        self._allowed_numbers = _parse_numbers(
            allowed_numbers or os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "")
        )
        self._bus = bus
        self._handlers: List[ChannelHandler] = []
        self._status = ChannelStatus.DISCONNECTED
        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._runtime_dir = _DEFAULT_RUNTIME_DIR
        self._last_qr: str = ""
        self._last_qr_svg: str = ""
        self._me: str = ""
        self._last_error: str = ""
        self._stderr_thread: Optional[threading.Thread] = None
        # Respawn the bridge if it dies; only once started via connect().
        self._supervise = False

    # -- bridge lifecycle -------------------------------------------------------

    def _ensure_bridge(self) -> Path:
        """Copy bundled bridge to runtime dir and run ``npm install`` if needed.

        Returns the path to ``dist/bridge.js``.

        Raises
        ------
        RuntimeError
            If ``node`` is not found on ``PATH``.
        """
        if shutil.which("node") is None:
            raise RuntimeError(
                "Node.js is required for WhatsAppBaileysChannel but 'node' "
                "was not found on PATH.  Install Node.js 22+ and try again."
            )

        prebuilt = os.environ.get(_PREBUILT_ENV, "")
        if prebuilt:
            bridge_js = Path(prebuilt) / "dist" / "bridge.js"
            if bridge_js.exists() and (Path(prebuilt) / "node_modules").exists():
                return bridge_js
            logger.warning(
                "%s=%s has no built bridge; falling back to npm install",
                _PREBUILT_ENV,
                prebuilt,
            )

        runtime = self._runtime_dir
        runtime.mkdir(parents=True, exist_ok=True)

        # Copy package.json + dist/ from bundled source if not already present,
        # or if the bundled version is newer.
        pkg_dst = runtime / "package.json"
        pkg_src = _BRIDGE_SRC / "package.json"
        if pkg_src.exists() and (
            not pkg_dst.exists() or pkg_src.stat().st_mtime > pkg_dst.stat().st_mtime
        ):
            shutil.copy2(pkg_src, pkg_dst)

        dist_dst = runtime / "dist"
        dist_src = _BRIDGE_SRC / "dist"
        if dist_src.exists():
            if dist_dst.exists():
                shutil.rmtree(dist_dst)
            shutil.copytree(dist_src, dist_dst)

        # Run npm install if node_modules is missing.
        node_modules = runtime / "node_modules"
        if not node_modules.exists():
            logger.info("Running npm install in %s", runtime)
            subprocess.run(
                ["npm", "install", "--production"],
                cwd=str(runtime),
                check=True,
                capture_output=True,
            )

        bridge_js = runtime / "dist" / "bridge.js"
        if not bridge_js.exists():
            raise RuntimeError(
                f"Bridge entry point not found at {bridge_js}.  "
                "Ensure the bridge TypeScript has been compiled."
            )
        return bridge_js

    # -- BaseChannel interface ---------------------------------------------------

    def connect(self) -> None:
        """Spawn the Node.js bridge subprocess and start the reader thread."""
        if self._status == ChannelStatus.CONNECTED:
            return

        self._status = ChannelStatus.CONNECTING

        try:
            bridge_js = self._ensure_bridge()
        except RuntimeError as exc:
            logger.error("Bridge setup failed: %s", exc)
            self._status = ChannelStatus.ERROR
            return

        auth = self._auth_dir or str(self._runtime_dir / "auth")
        cmd = ["node", str(bridge_js), "--auth-dir", auth]
        cmd += ["--mode", "own" if self._assistant_has_own_number else "self"]
        if self._allowed_numbers:
            cmd += ["--allowed", ",".join(self._allowed_numbers)]

        try:
            self._stop_event.clear()
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                daemon=True,
            )
            self._reader_thread.start()
            # An undrained stderr pipe fills up and blocks the bridge.
            self._stderr_thread = threading.Thread(
                target=self._stderr_loop,
                daemon=True,
            )
            self._stderr_thread.start()
            self._supervise = True
            logger.info(
                "WhatsApp Baileys bridge started (pid=%s)",
                self._process.pid,
            )
        except Exception:
            logger.exception("Failed to start bridge subprocess")
            self._status = ChannelStatus.ERROR

    def disconnect(self) -> None:
        """Send disconnect command to the bridge and terminate the subprocess."""
        self._supervise = False
        self._stop_event.set()

        if self._process is not None and self._process.stdin is not None:
            try:
                self._write_command({"type": "disconnect"})
            except Exception:
                logger.debug("Could not send disconnect command", exc_info=True)

        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5.0)
            except Exception:
                logger.debug("Bridge process termination error", exc_info=True)
            self._process = None

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5.0)
            self._reader_thread = None

        self._status = ChannelStatus.DISCONNECTED

    def send(
        self,
        channel: str,
        content: str,
        *,
        conversation_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> bool:
        """Send a message to a WhatsApp JID via the bridge subprocess."""
        if self._process is None or self._status != ChannelStatus.CONNECTED:
            logger.warning("Cannot send: bridge not connected")
            return False

        try:
            self._write_command(
                {
                    "type": "send",
                    "jid": channel,
                    "text": content,
                }
            )
            self._publish_sent(channel, content, conversation_id)
            return True
        except Exception:
            logger.debug("WhatsApp Baileys send failed", exc_info=True)
            return False

    def status(self) -> ChannelStatus:
        """Return the current connection status."""
        return self._status

    def list_channels(self) -> List[str]:
        """Return available channel identifiers."""
        return ["whatsapp_baileys"]

    def on_message(self, handler: ChannelHandler) -> None:
        """Register a callback for incoming messages."""
        self._handlers.append(handler)

    # -- pairing ------------------------------------------------------------------

    def pairing_info(self) -> Dict[str, Any]:
        """Return linking state for the pairing page.

        ``qr_svg`` is only present while waiting for the QR to be scanned.
        """
        connected = self._status == ChannelStatus.CONNECTED
        return {
            "status": self._status.value,
            "connected": connected,
            "me": self._me if connected else "",
            "mode": "own" if self._assistant_has_own_number else "self",
            "qr_svg": "" if connected else self._last_qr_svg,
            "error": self._last_error,
        }

    def logout(self) -> bool:
        """Unlink the WhatsApp device; the bridge then offers a new QR."""
        if self._process is None:
            return False
        try:
            self._write_command({"type": "logout"})
            return True
        except Exception:
            logger.debug("WhatsApp Baileys logout failed", exc_info=True)
            return False

    # -- internal helpers -------------------------------------------------------

    def _write_command(self, cmd: Dict[str, Any]) -> None:
        """Write a JSON-line command to the bridge's stdin."""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Bridge process not running")
        line = json.dumps(cmd, separators=(",", ":")) + "\n"
        self._process.stdin.write(line)
        self._process.stdin.flush()

    def _reader_loop(self) -> None:
        """Background thread: read JSON lines from bridge stdout."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return

        try:
            for raw_line in proc.stdout:
                if self._stop_event.is_set():
                    break

                line = raw_line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from bridge: %s", line)
                    continue

                self._handle_bridge_event(event)
        except Exception:
            if not self._stop_event.is_set():
                logger.debug("Reader loop error", exc_info=True)
                self._status = ChannelStatus.ERROR

        # stdout closed: the bridge exited.  Respawn unless we are stopping.
        if self._supervise and not self._stop_event.is_set() and self._process is proc:
            logger.warning(
                "WhatsApp Baileys bridge exited; restarting in %.0fs",
                _RESTART_DELAY_S,
            )
            self._status = ChannelStatus.ERROR
            if not self._stop_event.wait(_RESTART_DELAY_S):
                self._process = None
                self.connect()

    def _stderr_loop(self) -> None:
        """Background thread: forward bridge stderr to the log."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            for raw_line in proc.stderr:
                line = raw_line.rstrip()
                if line:
                    logger.debug("baileys: %s", line)
        except Exception:
            logger.debug("Stderr loop error", exc_info=True)

    def _handle_bridge_event(self, event: Dict[str, Any]) -> None:
        """Dispatch a single JSON event from the bridge."""
        event_type = event.get("type", "")

        if event_type == "status":
            new_status = event.get("status", "")
            if new_status == "connected":
                self._status = ChannelStatus.CONNECTED
                self._me = event.get("me", "")
                self._last_qr = ""
                self._last_qr_svg = ""
                self._last_error = ""
                logger.info("WhatsApp Baileys bridge connected")
            elif new_status in ("disconnected", "logged_out"):
                self._status = ChannelStatus.DISCONNECTED
                self._me = ""
                if new_status == "logged_out":
                    logger.info("WhatsApp device unlinked -- waiting for new QR")

        elif event_type == "qr":
            self._last_qr = event.get("data", "")
            self._last_qr_svg = event.get("svg", "")
            logger.info("WhatsApp QR code ready -- open /whatsapp to scan it")

        elif event_type == "message":
            cm = ChannelMessage(
                channel="whatsapp_baileys",
                sender=event.get("sender", ""),
                content=event.get("text", ""),
                message_id=event.get("message_id", ""),
                conversation_id=event.get("jid", ""),
            )
            for handler in self._handlers:
                try:
                    handler(cm)
                except Exception:
                    logger.exception("WhatsApp Baileys handler error")
            if self._bus is not None:
                self._bus.publish(
                    EventType.CHANNEL_MESSAGE_RECEIVED,
                    {
                        "channel": cm.channel,
                        "sender": cm.sender,
                        "content": cm.content,
                        "message_id": cm.message_id,
                    },
                )

        elif event_type == "error":
            message = event.get("message", "unknown")
            logger.error("Bridge error: %s", message)
            self._last_error = message
            # Send failures and config warnings do not take the link down.
            if message.startswith("Fatal"):
                self._status = ChannelStatus.ERROR

    def _publish_sent(self, channel: str, content: str, conversation_id: str) -> None:
        """Publish a CHANNEL_MESSAGE_SENT event on the bus."""
        if self._bus is not None:
            self._bus.publish(
                EventType.CHANNEL_MESSAGE_SENT,
                {
                    "channel": channel,
                    "content": content,
                    "conversation_id": conversation_id,
                },
            )


__all__ = ["WhatsAppBaileysChannel"]
