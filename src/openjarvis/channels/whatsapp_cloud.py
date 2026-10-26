"""WhatsApp Business (official Meta Cloud API) channel for a Jarvis instance.

Alternative to the Baileys "linked device" mode.  Jarvis gets its own
WhatsApp Business number; the owner talks to it from their personal
WhatsApp (``owner_number``), and anyone else who writes to that number lands
in the inbox and is forwarded to the owner.

Trade-offs the setup wizard explains to the user:

* a number registered on the Cloud API cannot also be used in the WhatsApp
  app on a phone at the same time (API *or* phone);
* Jarvis only sees messages sent to its business number, not the owner's
  personal chats;
* Meta only allows free-form messages within 24 h of the person's last
  message; outside that window an approved template is needed
  (``template_name``, optional, one body parameter).

Everything else -- the "sí" confirmation, reminders, email alerts, the
assistant's instructions -- is inherited from :class:`WhatsAppBaileysChannel`;
only the transport changes.  Settings come from the setup wizard
(``<home>/whatsapp_cloud.json``) or, failing that, environment variables
``META_WA_PHONE_NUMBER_ID``, ``META_WA_ACCESS_TOKEN``, ``META_WA_APP_SECRET``,
``WHATSAPP_OWNER_NUMBER`` and ``META_WA_TEMPLATE``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from openjarvis.channels import whatsapp_baileys as wb
from openjarvis.channels._stubs import ChannelMessage, ChannelStatus
from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import ChannelRegistry

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v21.0"
# Meta: "Re-engagement message" -- outside the 24 h customer service window.
_OUTSIDE_WINDOW = {131047, 131026}


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _wa_id(jid: str) -> str:
    """'34600111222@s.whatsapp.net' or '+34 600…' -> '34600111222'."""
    return _digits(str(jid).split("@", 1)[0])


@ChannelRegistry.register("whatsapp_cloud")
class WhatsAppCloudChannel(WhatsAppBaileysChannel):
    """WhatsApp through the official WhatsApp Business Cloud API."""

    channel_id = "whatsapp_cloud"

    def __init__(
        self,
        *,
        assistant_name: str = "",
        config_path: str | Path | None = None,
        http: Optional[httpx.Client] = None,
        store: Any = None,
        bus: Any = None,
        **_ignored: Any,
    ) -> None:
        super().__init__(
            assistant_name=assistant_name
            or os.environ.get("JARVIS_ASSISTANT_NAME", "Jarvis"),
            assistant_has_own_number=False,
            notify=os.environ.get("WHATSAPP_NOTIFY", "all"),
            confirm_sends=True,
            store=store,
            bus=bus,
        )
        self._config_path = Path(
            config_path or get_config_dir() / "whatsapp_cloud.json"
        )
        self._http = http or httpx.Client(timeout=20)
        self._cfg_lock = threading.Lock()
        self._cfg: Dict[str, str] = {}
        self._display_number = ""
        self._load_config()

    # -- configuration ------------------------------------------------------

    def _load_config(self) -> None:
        try:
            saved = json.loads(self._config_path.read_text(encoding="utf-8"))
        except Exception:
            saved = {}
        env = {
            "phone_number_id": os.environ.get("META_WA_PHONE_NUMBER_ID", ""),
            "access_token": os.environ.get("META_WA_ACCESS_TOKEN", ""),
            "app_secret": os.environ.get("META_WA_APP_SECRET", ""),
            "owner_number": _digits(os.environ.get("WHATSAPP_OWNER_NUMBER", "")),
            "template_name": os.environ.get("META_WA_TEMPLATE", ""),
        }
        cfg = {k: saved.get(k) or v for k, v in env.items()}
        # The webhook verify token is ours: generate it once and keep it.
        cfg["verify_token"] = saved.get("verify_token") or secrets.token_urlsafe(24)
        with self._cfg_lock:
            self._cfg = cfg
        if not saved.get("verify_token"):
            self._save_config()

    def _save_config(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cfg), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self._config_path)

    @property
    def configured(self) -> bool:
        c = self._cfg
        return bool(
            c.get("phone_number_id") and c.get("access_token") and c.get("owner_number")
        )

    @property
    def verify_token(self) -> str:
        return self._cfg.get("verify_token", "")

    def configure(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        owner_number: str,
        app_secret: str = "",
        template_name: str = "",
    ) -> Dict[str, Any]:
        """Validate credentials against Meta and store them."""
        phone_number_id = _digits(phone_number_id)
        # Re-saving without retyping the secrets keeps the stored ones.
        access_token = access_token.strip() or self._cfg.get("access_token", "")
        owner = _digits(owner_number)
        if len(owner) == 9:
            owner = "34" + owner  # Spanish mobile without prefix
        if not phone_number_id or not access_token.strip() or len(owner) < 10:
            raise ValueError("Faltan datos: ID del número, token y tu número personal.")
        info = self._fetch_number(phone_number_id, access_token.strip())
        with self._cfg_lock:
            self._cfg.update(
                phone_number_id=phone_number_id,
                access_token=access_token.strip(),
                owner_number=owner,
                app_secret=app_secret.strip() or self._cfg.get("app_secret", ""),
                template_name=template_name.strip(),
            )
            self._save_config()
        self._mark_connected(info)
        return self.pairing_info()

    def _fetch_number(self, phone_number_id: str, token: str) -> Dict[str, Any]:
        resp = self._http.get(
            f"{GRAPH}/{phone_number_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating"},
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            msg = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
            raise ValueError(f"Meta rechazó los datos: {msg}")
        return data

    def _mark_connected(self, info: Dict[str, Any]) -> None:
        self._display_number = _digits(info.get("display_phone_number", ""))
        self._status = ChannelStatus.CONNECTED
        self._me = self._display_number
        self._last_error = ""

    # -- lifecycle -------------------------------------------------------------

    def connect(self) -> None:
        """Check the credentials (if any) and start handling messages."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="wa-cloud"
            )
        wb._active_channel = self
        if not self.configured:
            self._status = ChannelStatus.DISCONNECTED
            return
        try:
            self._mark_connected(
                self._fetch_number(
                    self._cfg["phone_number_id"], self._cfg["access_token"]
                )
            )
        except Exception as exc:
            self._status = ChannelStatus.ERROR
            self._last_error = str(exc)[:200]
            logger.warning("WhatsApp Cloud API check failed: %s", exc)

    def disconnect(self) -> None:
        self._status = ChannelStatus.DISCONNECTED
        if wb._active_channel is self:
            wb._active_channel = None

    def logout(self) -> bool:
        """Forget the Meta credentials (the business number stays on Meta)."""
        with self._cfg_lock:
            for key in (
                "phone_number_id",
                "access_token",
                "app_secret",
                "template_name",
            ):
                self._cfg[key] = ""
            self._save_config()
        self._status = ChannelStatus.DISCONNECTED
        self._me = self._display_number = ""
        return True

    def request_pairing_code(self, phone: str) -> bool:  # not applicable
        return False

    # -- identity --------------------------------------------------------------

    def self_jid(self) -> str:
        """The owner's chat: where Jarvis answers, confirms and notifies."""
        owner = self._cfg.get("owner_number", "")
        return f"{owner}@s.whatsapp.net" if owner and self.configured else ""

    def pairing_info(self) -> Dict[str, Any]:
        connected = self._status == ChannelStatus.CONNECTED
        return {
            "provider": "meta",
            "status": self._status.value,
            "connected": connected,
            "me": self._display_number if connected else "",
            "owner": self._cfg.get("owner_number", ""),
            "configured": self.configured,
            "phone_number_id": self._cfg.get("phone_number_id", ""),
            "has_app_secret": bool(self._cfg.get("app_secret")),
            "template_name": self._cfg.get("template_name", ""),
            "verify_token": self.verify_token,
            "mode": "meta",
            "qr_svg": "",
            "pairing_code": "",
            "pairing_error": "",
            "error": self._last_error,
        }

    @property
    def system_prompt(self) -> str:
        prompt = super().system_prompt
        return prompt + (
            "\n\nModo WhatsApp Business oficial: tienes tu propio número de "
            "empresa y el usuario te escribe desde su WhatsApp personal. "
            "whatsapp_read_messages solo ve los mensajes que otras personas "
            "envían a TU número, no los chats personales del usuario. Solo "
            "puedes escribir a alguien que haya escrito a tu número en las "
            "últimas 24 horas; si no, explícaselo al usuario."
        )

    # -- sending ---------------------------------------------------------------

    def _post_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._http.post(
            f"{GRAPH}/{self._cfg['phone_number_id']}/messages",
            json={"messaging_product": "whatsapp", **payload},
            headers={"Authorization": f"Bearer {self._cfg['access_token']}"},
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 300:
            err = data.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code"),
                "error": err.get("error_user_msg")
                or err.get("message")
                or f"HTTP {resp.status_code}",
            }
        return {"ok": True, "error": ""}

    def send_and_wait(
        self, jid: str, text: str, *, timeout: float = 20.0
    ) -> Dict[str, Any]:
        if not self.configured:
            return {
                "ok": False,
                "jid": jid,
                "error": "WhatsApp Business no está configurado",
            }
        to = _wa_id(jid)
        try:
            result = self._post_message(
                {
                    "to": to,
                    "type": "text",
                    "text": {"body": text[:4096], "preview_url": False},
                }
            )
            template = self._cfg.get("template_name", "")
            if not result["ok"] and result.get("code") in _OUTSIDE_WINDOW and template:
                result = self._post_message(
                    {
                        "to": to,
                        "type": "template",
                        "template": {
                            "name": template,
                            "language": {"code": "es"},
                            "components": [
                                {
                                    "type": "body",
                                    "parameters": [
                                        {"type": "text", "text": text[:1000]}
                                    ],
                                }
                            ],
                        },
                    }
                )
            elif not result["ok"] and result.get("code") in _OUTSIDE_WINDOW:
                result["error"] = (
                    "Han pasado más de 24 h desde su último mensaje a tu número: "
                    "WhatsApp solo permite escribirle con una plantilla aprobada."
                )
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:200]}
        return {"ok": result["ok"], "jid": jid, "error": result.get("error", "")}

    def send(
        self,
        channel: str,
        content: str,
        *,
        conversation_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> bool:
        if not self.configured:
            return False
        ok = self.send_and_wait(channel, content)["ok"]
        if ok:
            self._publish_sent(channel, content, conversation_id)
        else:
            logger.warning("WhatsApp Cloud send failed")
        return ok

    # -- webhook ---------------------------------------------------------------

    def verify_signature(self, body: bytes, header: str) -> bool:
        secret = self._cfg.get("app_secret", "")
        if not secret:
            return False
        expected = (
            "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        )
        return hmac.compare_digest(expected, header or "")

    def handle_webhook(self, payload: Dict[str, Any]) -> int:
        """Process a Meta webhook payload; return how many messages it had."""
        handled = 0
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                if (value.get("metadata") or {}).get("phone_number_id") not in (
                    None,
                    self._cfg.get("phone_number_id"),
                ):
                    continue
                names = {
                    c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                    for c in value.get("contacts") or []
                }
                for msg in value.get("messages") or []:
                    handled += 1
                    self._handle_message(msg, names.get(msg.get("from"), ""))
        return handled

    @staticmethod
    def _text_of(msg: Dict[str, Any]) -> str:
        kind = msg.get("type", "")
        if kind == "text":
            return (msg.get("text") or {}).get("body", "")
        if kind == "button":
            return (msg.get("button") or {}).get("text", "")
        if kind == "interactive":
            inter = msg.get("interactive") or {}
            reply = inter.get("button_reply") or inter.get("list_reply") or {}
            return reply.get("title", "")
        labels = {
            "image": "[imagen]",
            "video": "[vídeo]",
            "audio": "[audio]",
            "document": "[documento]",
            "sticker": "[sticker]",
            "location": "[ubicación]",
            "contacts": "[contacto]",
        }
        caption = (
            (msg.get(kind) or {}).get("caption", "")
            if isinstance(msg.get(kind), dict)
            else ""
        )
        label = labels.get(kind, "[mensaje]")
        return f"{label} {caption}".strip()

    def _audio_event(self, msg: Dict[str, Any], jid: str) -> Dict[str, Any]:
        """Download an audio message into the bridge's ``audio`` event shape."""
        import base64

        audio = msg.get("audio") or {}
        event: Dict[str, Any] = {
            "jid": jid,
            "sender": jid,
            "message_id": msg.get("id", ""),
            "seconds": 0,
            "mimetype": audio.get("mime_type", "audio/ogg"),
            "ptt": bool(audio.get("voice")),
            "forwarded": bool((msg.get("context") or {}).get("forwarded")),
        }
        headers = {"Authorization": f"Bearer {self._cfg.get('access_token', '')}"}
        try:
            meta = self._http.get(f"{GRAPH}/{audio.get('id', '')}", headers=headers)
            meta.raise_for_status()
            info = meta.json()
            if int(info.get("file_size") or 0) > 20 * 1024 * 1024:
                event["error"] = "too_long"
                return event
            media = self._http.get(info["url"], headers=headers)
            media.raise_for_status()
            event["data"] = base64.b64encode(media.content).decode()
        except Exception:
            logger.warning("WhatsApp Cloud audio download failed", exc_info=True)
            event["error"] = "download_failed"
        return event

    def _handle_message(self, msg: Dict[str, Any], push_name: str) -> None:
        sender = _digits(msg.get("from", ""))
        text = self._text_of(msg)
        if not sender or not text:
            return
        jid = f"{sender}@s.whatsapp.net"
        if msg.get("type") == "audio" and sender == self._cfg.get("owner_number"):

            def run() -> None:
                self._handle_audio(self._audio_event(msg, jid))

            if self._executor is not None:
                self._executor.submit(run)
            else:
                run()
            return
        if sender == self._cfg.get("owner_number"):
            cm = ChannelMessage(
                channel=self.channel_id,
                sender=jid,
                content=text,
                message_id=msg.get("id", ""),
                conversation_id=jid,
            )
            if self._executor is not None:
                self._executor.submit(self._dispatch_message, cm)
            else:
                self._dispatch_message(cm)
            return
        # Someone else wrote to Jarvis's business number: inbox + notify owner.
        try:
            self._handle_inbox(
                {
                    "chat_jid": jid,
                    "sender_jid": jid,
                    "phone": sender,
                    "push_name": push_name,
                    "text": text,
                    "message_id": msg.get("id", ""),
                    "ts": float(msg.get("timestamp") or time.time()),
                    "is_group": False,
                    "from_me": False,
                }
            )
        except Exception:
            logger.exception("WhatsApp Cloud inbox handling failed")

    # Baileys-only plumbing that must never run here.
    def _write_command(self, cmd: Dict[str, Any]) -> None:  # pragma: no cover
        raise RuntimeError("Not available with the WhatsApp Cloud API")


def find_cloud_channel(bridge: Any) -> Optional[WhatsAppCloudChannel]:
    """The cloud channel inside a channel bridge (or the bridge itself)."""
    if isinstance(bridge, WhatsAppCloudChannel):
        return bridge
    channels = getattr(bridge, "_channels", None) or {}
    found = channels.get("whatsapp_cloud")
    if isinstance(found, WhatsAppCloudChannel):
        return found
    active = wb.get_active_channel()
    return active if isinstance(active, WhatsAppCloudChannel) else None


__all__: List[str] = ["WhatsAppCloudChannel", "find_cloud_channel"]
