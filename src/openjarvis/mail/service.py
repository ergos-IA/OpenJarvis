"""MailService: the user's mail accounts, new-mail notifications, short ids.

Accounts (at most one Gmail and one Outlook per instance) are linked from the
``/correo`` page and stored under ``<OPENJARVIS_HOME>/mail/``:

* ``gmail.json``      address + app password
* ``microsoft.json``  OAuth tokens (refresh token) for Microsoft Graph
* ``state.json``      per-account position of the new-mail poller

Environment:

* ``MS_CLIENT_ID`` / ``MS_TENANT_ID``  Entra app used to sign in to Outlook
* ``EMAIL_NOTIFY``        ``important`` (default: Gmail Primary tab, Outlook
  Focused inbox), ``all`` or ``none``
* ``EMAIL_POLL_SECONDS``  how often to look for new mail (default 120)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from openjarvis.core.paths import get_config_dir
from openjarvis.mail.accounts import GmailAccount, MicrosoftAuth, OutlookAccount

logger = logging.getLogger(__name__)

_NOTIFY_MODES = ("important", "all", "none")
_ID_PREFIX = {"gmail": "g", "outlook": "o"}


def _write_private(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


class MailService:
    """Holds the linked accounts and runs the new-mail poller."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        *,
        gmail_factory: Callable[[str, str], Any] = GmailAccount,
        notifier: Optional[Callable[[str], bool]] = None,
    ) -> None:
        env = os.environ if env is None else env
        self._dir = Path(data_dir) if data_dir else get_config_dir() / "mail"
        self._gmail_factory = gmail_factory
        self._notifier = notifier or _notify_whatsapp
        notify = (env.get("EMAIL_NOTIFY") or "important").strip().lower()
        self.notify_mode = notify if notify in _NOTIFY_MODES else "important"
        try:
            self.poll_seconds = max(30, int(env.get("EMAIL_POLL_SECONDS") or 120))
        except ValueError:
            self.poll_seconds = 120
        self._lock = threading.Lock()
        self._gmail: Optional[Any] = None
        creds = _read_json(self._dir / "gmail.json")
        if creds.get("email") and creds.get("app_password"):
            self._gmail = gmail_factory(creds["email"], creds["app_password"])
        client_id = (env.get("MS_CLIENT_ID") or "").strip()
        self.ms_auth: Optional[MicrosoftAuth] = None
        if client_id:
            self.ms_auth = MicrosoftAuth(
                client_id,
                (env.get("MS_TENANT_ID") or "common").strip(),
                self._dir / "microsoft.json",
            )
        self._outlook: Optional[OutlookAccount] = None
        # Short ids ("o3") for the model instead of 150-char Graph ids.
        self._ids: Dict[str, tuple] = {}
        self._next_id = 1
        self._errors: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- accounts ---------------------------------------------------------

    def accounts(self) -> Dict[str, Any]:
        """Linked accounts by kind ("gmail", "outlook")."""
        out: Dict[str, Any] = {}
        if self._gmail is not None:
            out["gmail"] = self._gmail
        if self.ms_auth is not None and self.ms_auth.connected:
            if self._outlook is None:
                self._outlook = OutlookAccount(self.ms_auth)
            out["outlook"] = self._outlook
        return out

    def account(self, kind: str = "") -> Any:
        """The account *kind*, or the only linked one if *kind* is empty."""
        linked = self.accounts()
        kind = (kind or "").strip().lower()
        if kind:
            if kind not in linked:
                raise LookupError(f"La cuenta {kind} no está conectada")
            return linked[kind]
        if len(linked) == 1:
            return next(iter(linked.values()))
        if not linked:
            raise LookupError("No hay ninguna cuenta de correo conectada")
        raise LookupError("Indica la cuenta: " + " o ".join(linked))

    def connect_gmail(self, email_address: str, app_password: str) -> None:
        account = self._gmail_factory(email_address, app_password)
        account.verify()
        _write_private(
            self._dir / "gmail.json",
            {"email": account.email, "app_password": app_password.replace(" ", "")},
        )
        with self._lock:
            self._gmail = account
            self._state_reset("gmail")

    def disconnect_gmail(self) -> None:
        with self._lock:
            self._gmail = None
            (self._dir / "gmail.json").unlink(missing_ok=True)
            self._state_reset("gmail")

    def disconnect_outlook(self) -> None:
        if self.ms_auth is not None:
            self.ms_auth.logout()
        with self._lock:
            self._outlook = None
            self._state_reset("outlook")

    def status(self) -> Dict[str, Any]:
        gmail = {"state": "disconnected"}
        if self._gmail is not None:
            gmail = {"state": "connected", "email": self._gmail.email}
        if self.ms_auth is None:
            outlook: Dict[str, Any] = {"state": "unavailable"}
        else:
            outlook = self.ms_auth.status()
        for kind, info in (("gmail", gmail), ("outlook", outlook)):
            if self._errors.get(kind) and info.get("state") == "connected":
                info["warning"] = self._errors[kind]
        return {"gmail": gmail, "outlook": outlook, "notify": self.notify_mode}

    # -- short ids --------------------------------------------------------

    def short_id(self, kind: str, message_id: str) -> str:
        with self._lock:
            for key, value in self._ids.items():
                if value == (kind, message_id):
                    return key
            key = f"{_ID_PREFIX.get(kind, 'm')}{self._next_id}"
            self._next_id += 1
            self._ids[key] = (kind, message_id)
            return key

    def resolve_id(self, short: str) -> tuple:
        """``(kind, real_id)`` for a short id from a previous listing."""
        found = self._ids.get((short or "").strip().lower())
        if found is None:
            raise LookupError(
                "No reconozco ese identificador de correo; vuelve a listar los correos"
            )
        return found

    # -- new-mail poller --------------------------------------------------

    def _state_path(self) -> Path:
        return self._dir / "state.json"

    def _state_reset(self, kind: str) -> None:
        state = _read_json(self._state_path())
        if state.pop(kind, None) is not None:
            _write_private(self._state_path(), state)

    def poll_once(self) -> int:
        """Check every account for new mail; return how many were notified."""
        if self.notify_mode == "none":
            return 0
        state = _read_json(self._state_path())
        sent = 0
        for kind, account in self.accounts().items():
            try:
                new, state[kind] = account.poll_new(
                    state.get(kind) or {}, self.notify_mode == "important"
                )
                self._errors.pop(kind, None)
            except Exception as exc:
                self._errors[kind] = str(exc)[:200]
                logger.warning("Mail poll failed for %s: %s", kind, exc)
                continue
            for msg in new:
                if self._notifier(self.format_notification(account, msg)):
                    sent += 1
        _write_private(self._state_path(), state)
        return sent

    def format_notification(self, account: Any, msg: Dict[str, Any]) -> str:
        sid = self.short_id(account.kind, msg["id"])
        sender = msg["from_name"]
        if msg["from_addr"] and msg["from_addr"] != sender:
            sender = f"{sender} <{msg['from_addr']}>"
        lines = [f"📧 *{account.label}* · {sender}", f"*{msg['subject']}*"]
        if msg.get("preview"):
            lines.append(msg["preview"][:240])
        lines.append(f"_(ref. {sid})_")
        return "\n".join(lines)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="mail-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.poll_once()
            except Exception:
                logger.exception("Mail poller iteration failed")


def _notify_whatsapp(text: str) -> bool:
    """Send *text* to the user's own WhatsApp chat, if WhatsApp is linked."""
    from openjarvis.channels.whatsapp_baileys import get_active_channel

    channel = get_active_channel()
    own = channel.self_jid() if channel is not None else ""
    if not own:
        return False
    return bool(channel.send(own, text))


def format_time(ts: float) -> str:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(os.environ.get("TZ") or "Europe/Madrid")
    except Exception:
        tz = None
    return datetime.fromtimestamp(ts, tz).strftime("%d/%m %H:%M")


_service: Optional[MailService] = None
_service_lock = threading.Lock()


def get_mail_service() -> MailService:
    """Process-wide MailService (created on first use)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = MailService()
        return _service


def reset_mail_service() -> None:
    """Drop the singleton (tests)."""
    global _service
    with _service_lock:
        if _service is not None:
            _service.stop()
        _service = None


__all__ = ["MailService", "format_time", "get_mail_service", "reset_mail_service"]
