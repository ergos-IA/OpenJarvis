"""Mail accounts: Gmail over IMAP/SMTP and Outlook / Microsoft 365 over Graph.

Both expose the same small interface used by :class:`MailService` and the
email tools:

``list_messages(query, unread_only, since_hours, limit)``
    Newest first.  Each message is a dict with ``id``, ``from_name``,
    ``from_addr``, ``subject``, ``date`` (epoch seconds), ``preview`` and
    ``unread``.
``read(message_id)``
    The same fields plus ``to``, ``body`` (plain text) and threading headers.
``send(to, subject, body, reply_to_id="")``
    Returns ``{"ok": bool, "error": str}``.
``poll_new(state, important_only)``
    ``(new_messages, new_state)``.  The first call (``state`` empty) only
    records the current position, so connecting never floods the user.
"""

from __future__ import annotations

import email as email_lib
import html
import imaplib
import json
import logging
import re
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from openjarvis.connectors.gmail_imap import _decode_header, _extract_text_body

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30
_PREVIEW_CHARS = 300
# Never download more than this many new messages per poll.
_MAX_NEW_PER_POLL = 10


def html_to_text(raw: str) -> str:
    """Crude HTML -> text, enough for an LLM to read an email."""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean(text: str, limit: int = 0) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] if limit else text


def _body_text(msg: email_lib.message.Message) -> str:
    """Plain-text body; HTML-only mails are converted."""
    plain = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/plain":
            plain = _extract_text_body(part)
            break
    if plain:
        return plain.strip()
    return html_to_text(_extract_text_body(msg))


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


def _parse_fetch(data: List[Any]) -> Dict[str, Dict[str, Any]]:
    """Group an IMAP FETCH response by UID: {uid: {"meta": str, "raw": bytes}}."""
    out: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None
    for item in data or []:
        if isinstance(item, tuple):
            meta = item[0].decode(errors="replace")
            uid = re.search(r"UID (\d+)", meta)
            current = out.setdefault(uid.group(1) if uid else meta, {"meta": ""})
            current["meta"] += meta
            current["raw"] = item[1]
        elif isinstance(item, bytes) and current is not None:
            # Some servers put FLAGS etc. after the literal.
            current["meta"] += item.decode(errors="replace")
    return out


class GmailAccount:
    """Gmail with an app password (requires 2-step verification)."""

    kind = "gmail"
    label = "Gmail"
    imap_host = "imap.gmail.com"
    smtp_host = "smtp.gmail.com"

    def __init__(
        self,
        email_address: str,
        app_password: str,
        *,
        imap_factory: Optional[Callable[[], imaplib.IMAP4]] = None,
        smtp_factory: Optional[Callable[[], smtplib.SMTP]] = None,
    ) -> None:
        self.email = email_address.strip()
        # Google shows app passwords in groups of four ("abcd efgh ...").
        self._password = app_password.replace(" ", "").strip()
        self._imap_factory = imap_factory or (
            lambda: imaplib.IMAP4_SSL(self.imap_host, 993, timeout=_TIMEOUT_S)
        )
        self._smtp_factory = smtp_factory or (
            lambda: smtplib.SMTP_SSL(self.smtp_host, 465, timeout=_TIMEOUT_S)
        )
        self._lock = threading.Lock()

    # -- connection -------------------------------------------------------

    def _open(self) -> imaplib.IMAP4:
        imap = self._imap_factory()
        imap.login(self.email, self._password)
        return imap

    @staticmethod
    def _close(imap: imaplib.IMAP4) -> None:
        try:
            imap.logout()
        except Exception:
            pass

    def verify(self) -> None:
        """Raise if the credentials don't open the inbox."""
        imap = self._open()
        try:
            typ, _ = imap.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError("No se pudo abrir la bandeja de entrada")
        finally:
            self._close(imap)

    @staticmethod
    def _all_mail(imap: imaplib.IMAP4) -> str:
        """Name of the "All Mail" folder (localised, e.g. "[Gmail]/Todos")."""
        typ, data = imap.list()
        for line in data or []:
            text = line.decode(errors="replace") if isinstance(line, bytes) else ""
            if "\\All" in text:
                match = re.search(r'"([^"]+)"\s*$', text)
                if match:
                    return match.group(1)
        return "INBOX"

    @staticmethod
    def _select(imap: imaplib.IMAP4, mailbox: str) -> None:
        typ, _ = imap.select('"%s"' % mailbox.replace('"', '\\"'), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"No se pudo abrir {mailbox}")

    @staticmethod
    def _gm_search(imap: imaplib.IMAP4, raw_query: str) -> List[bytes]:
        """UID SEARCH with Gmail's own search syntax (UTF-8 safe)."""
        imap.literal = raw_query.encode("utf-8")
        typ, data = imap.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        if typ != "OK":
            raise RuntimeError("La búsqueda en Gmail falló")
        return (data[0] or b"").split()

    def _fetch(
        self, imap: imaplib.IMAP4, uids: List[bytes], *, full: bool
    ) -> List[Dict[str, Any]]:
        if not uids:
            return []
        part = (
            "BODY.PEEK[]"
            if full
            else (
                "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID REFERENCES)]"
            )
        )
        typ, data = imap.uid(
            "FETCH", b",".join(uids).decode(), f"(UID X-GM-MSGID FLAGS {part})"
        )
        if typ != "OK":
            raise RuntimeError("No se pudieron leer los correos")
        messages = []
        for item in _parse_fetch(data).values():
            msg = email_lib.message_from_bytes(item.get("raw") or b"")
            gm_id = re.search(r"X-GM-MSGID (\d+)", item["meta"])
            name, addr = parseaddr(_decode_header(msg.get("From", "")))
            try:
                date = parsedate_to_datetime(msg.get("Date", "")).timestamp()
            except Exception:
                date = time.time()
            entry = {
                "id": gm_id.group(1) if gm_id else "",
                "from_name": name or addr,
                "from_addr": addr,
                "subject": _decode_header(msg.get("Subject", "")) or "(sin asunto)",
                "date": date,
                "unread": "\\Seen" not in item["meta"],
                "preview": "",
                "to": ", ".join(
                    a for _, a in getaddresses([_decode_header(msg.get("To", ""))])
                ),
                "message_id": msg.get("Message-ID", ""),
                "references": msg.get("References", ""),
            }
            if full:
                entry["body"] = _body_text(msg)
                entry["preview"] = _clean(entry["body"], _PREVIEW_CHARS)
            messages.append(entry)
        messages.sort(key=lambda m: m["date"], reverse=True)
        return messages

    # -- interface --------------------------------------------------------

    def list_messages(
        self,
        query: str = "",
        unread_only: bool = False,
        since_hours: float = 0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        terms = [query.strip()] if query.strip() else ["in:inbox"]
        if unread_only:
            terms.append("is:unread")
        if since_hours:
            terms.append(f"newer_than:{max(1, int(-(-since_hours // 24)))}d")
        with self._lock:
            imap = self._open()
            try:
                self._select(imap, self._all_mail(imap))
                uids = self._gm_search(imap, " ".join(terms))
                # UIDs grow with arrival time in All Mail: the tail is newest.
                messages = self._fetch(imap, uids[-limit:], full=False)
            finally:
                self._close(imap)
        if since_hours:
            cutoff = time.time() - since_hours * 3600
            messages = [m for m in messages if m["date"] >= cutoff]
        return messages

    def read(self, message_id: str) -> Dict[str, Any]:
        if not message_id.isdigit():
            raise ValueError("Identificador de Gmail no válido")
        with self._lock:
            imap = self._open()
            try:
                self._select(imap, self._all_mail(imap))
                typ, data = imap.uid("SEARCH", "X-GM-MSGID", message_id)
                uids = (data[0] or b"").split() if typ == "OK" else []
                found = self._fetch(imap, uids[-1:], full=True)
            finally:
                self._close(imap)
        if not found:
            raise LookupError("No encuentro ese correo")
        return found[0]

    def send(
        self, to: str, subject: str, body: str, reply_to_id: str = ""
    ) -> Dict[str, Any]:
        msg = EmailMessage()
        msg["From"] = self.email
        if reply_to_id:
            original = self.read(reply_to_id)
            to = to or original["from_addr"]
            subject = subject or original["subject"]
            if not re.match(r"(?i)^(re|aw|rv):", subject):
                subject = f"Re: {subject}"
            if original["message_id"]:
                msg["In-Reply-To"] = original["message_id"]
                msg["References"] = (
                    f"{original['references']} {original['message_id']}".strip()
                )
        msg["To"] = to
        msg["Subject"] = subject or "(sin asunto)"
        msg.set_content(body)
        try:
            smtp = self._smtp_factory()
            try:
                smtp.login(self.email, self._password)
                smtp.send_message(msg)
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "error": ""}

    def poll_new(
        self, state: Dict[str, Any], important_only: bool
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        with self._lock:
            imap = self._open()
            try:
                typ, _ = imap.select("INBOX", readonly=True)
                if typ != "OK":
                    raise RuntimeError("No se pudo abrir la bandeja de entrada")
                validity = (imap.response("UIDVALIDITY")[1] or [b""])[0]
                validity = validity.decode() if isinstance(validity, bytes) else ""
                typ, data = imap.uid("SEARCH", None, "ALL")
                uids = [int(u) for u in (data[0] or b"").split()] if typ == "OK" else []
                last = max(uids, default=0)
                if not state or state.get("uidvalidity") != validity:
                    return [], {"uidvalidity": validity, "last_uid": last}
                seen = int(state.get("last_uid", 0))
                new = [u for u in uids if u > seen]
                if new and important_only:
                    # Skip Promotions/Social/Updates: only the Primary tab.
                    primary = {
                        int(u)
                        for u in self._gm_search(
                            imap, "in:inbox category:primary newer_than:2d"
                        )
                    }
                    new = [u for u in new if u in primary]
                fetched = self._fetch(
                    imap,
                    [str(u).encode() for u in new[-_MAX_NEW_PER_POLL:]],
                    full=True,
                )
            finally:
                self._close(imap)
        return fetched, {"uidvalidity": validity, "last_uid": max(last, seen)}


# ---------------------------------------------------------------------------
# Microsoft 365 / Outlook
# ---------------------------------------------------------------------------

_GRAPH = "https://graph.microsoft.com/v1.0"
_MS_SCOPES = "offline_access User.Read Mail.ReadWrite Mail.Send"
_MS_SELECT = (
    "id,subject,from,receivedDateTime,isRead,bodyPreview,inferenceClassification"
)


class MicrosoftAuth:
    """OAuth device-code sign-in for one Microsoft account, tokens on disk.

    The user opens ``verification_uri`` (microsoft.com/devicelogin), types the
    short code and signs in; no redirect URL or client secret is needed.  The
    Entra app must allow public client flows and request the delegated
    permissions in ``_MS_SCOPES``.
    """

    def __init__(
        self,
        client_id: str,
        tenant: str,
        token_path: Path,
        *,
        http: Optional[httpx.Client] = None,
    ) -> None:
        self.client_id = client_id
        self.tenant = tenant or "common"
        self._path = Path(token_path)
        self._http = http or httpx.Client(timeout=_TIMEOUT_S)
        self._lock = threading.Lock()
        self._tokens: Dict[str, Any] = self._load()
        self._flow: Dict[str, Any] = {}
        self._flow_thread: Optional[threading.Thread] = None

    @property
    def _authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0"

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tokens), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self._path)

    # -- status -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self._tokens.get("refresh_token"))

    @property
    def email(self) -> str:
        return self._tokens.get("email", "")

    def status(self) -> Dict[str, Any]:
        if self.connected:
            return {"state": "connected", "email": self.email}
        flow = dict(self._flow)
        if flow.get("state") == "pending" and flow.get("expires", 0) < time.time():
            flow = {"state": "error", "error": "El código ha caducado"}
        return {
            "state": flow.get("state", "disconnected"),
            "user_code": flow.get("user_code", ""),
            "verification_uri": flow.get("verification_uri", ""),
            "error": flow.get("error", ""),
        }

    # -- device flow ------------------------------------------------------

    def start_device_flow(self) -> Dict[str, Any]:
        resp = self._http.post(
            f"{self._authority}/devicecode",
            data={"client_id": self.client_id, "scope": _MS_SCOPES},
        )
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(data.get("error_description") or data.get("error"))
        self._flow = {
            "state": "pending",
            "user_code": data["user_code"],
            "verification_uri": data["verification_uri"],
            "device_code": data["device_code"],
            "interval": int(data.get("interval", 5)),
            "expires": time.time() + int(data.get("expires_in", 900)),
        }
        self._flow_thread = threading.Thread(
            target=self._wait_for_sign_in,
            args=(self._flow,),
            name="ms-device-flow",
            daemon=True,
        )
        self._flow_thread.start()
        return self.status()

    def _wait_for_sign_in(self, flow: Dict[str, Any]) -> None:
        interval = flow["interval"]
        while flow is self._flow and time.time() < flow["expires"]:
            time.sleep(interval)
            try:
                resp = self._http.post(
                    f"{self._authority}/token",
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "client_id": self.client_id,
                        "device_code": flow["device_code"],
                    },
                )
                data = resp.json()
            except Exception as exc:
                logger.warning("Microsoft sign-in poll failed: %s", exc)
                continue
            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error:
                flow["state"] = "error"
                flow["error"] = data.get("error_description", error).split("\r\n")[0]
                return
            with self._lock:
                self._store_tokens(data)
                try:
                    me = self._http.get(
                        f"{_GRAPH}/me",
                        headers={"Authorization": f"Bearer {data['access_token']}"},
                    ).json()
                    self._tokens["email"] = me.get("mail") or me.get(
                        "userPrincipalName", ""
                    )
                except Exception:
                    logger.debug("Could not read Microsoft profile", exc_info=True)
                self._save()
            flow["state"] = "done"
            return
        if flow is self._flow and flow.get("state") == "pending":
            flow["state"] = "error"
            flow["error"] = "El código ha caducado"

    def _store_tokens(self, data: Dict[str, Any]) -> None:
        self._tokens["access_token"] = data["access_token"]
        self._tokens["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        if data.get("refresh_token"):
            self._tokens["refresh_token"] = data["refresh_token"]

    def access_token(self) -> str:
        with self._lock:
            if not self.connected:
                raise RuntimeError("Outlook no está conectado")
            if self._tokens.get("expires_at", 0) - 120 > time.time():
                return self._tokens["access_token"]
            resp = self._http.post(
                f"{self._authority}/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "refresh_token": self._tokens["refresh_token"],
                    "scope": _MS_SCOPES,
                },
            )
            data = resp.json()
            if resp.status_code != 200 or "access_token" not in data:
                if data.get("error") == "invalid_grant":
                    # Revoked or expired: the user has to sign in again.
                    self._tokens = {}
                    self._path.unlink(missing_ok=True)
                raise RuntimeError(
                    "No se pudo renovar el acceso a Outlook: "
                    + str(data.get("error_description", data.get("error", "")))[:200]
                )
            self._store_tokens(data)
            self._save()
            return self._tokens["access_token"]

    def logout(self) -> None:
        with self._lock:
            self._tokens = {}
            self._flow = {}
            self._path.unlink(missing_ok=True)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _graph_ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


class OutlookAccount:
    """Outlook.com or Microsoft 365 mailbox through Microsoft Graph."""

    kind = "outlook"
    label = "Outlook"

    def __init__(self, auth: MicrosoftAuth, *, http: Optional[httpx.Client] = None):
        self._auth = auth
        self._http = http or httpx.Client(timeout=_TIMEOUT_S)

    @property
    def email(self) -> str:
        return self._auth.email

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._auth.access_token()}"
        resp = self._http.request(method, f"{_GRAPH}{path}", headers=headers, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(f"Outlook ({resp.status_code}): {detail}")
        return resp

    @staticmethod
    def _summary(item: Dict[str, Any]) -> Dict[str, Any]:
        sender = (item.get("from") or {}).get("emailAddress") or {}
        return {
            "id": item.get("id", ""),
            "from_name": sender.get("name") or sender.get("address", ""),
            "from_addr": sender.get("address", ""),
            "subject": item.get("subject") or "(sin asunto)",
            "date": _graph_ts(item.get("receivedDateTime", "")),
            "unread": not item.get("isRead", True),
            "preview": _clean(item.get("bodyPreview", ""), _PREVIEW_CHARS),
        }

    def list_messages(
        self,
        query: str = "",
        unread_only: bool = False,
        since_hours: float = 0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        cutoff = time.time() - since_hours * 3600 if since_hours else 0
        if query.strip():
            # $search can't be combined with $filter/$orderby: filter here.
            params = {
                "$search": '"%s"' % " ".join(query.replace('"', " ").split()),
                "$top": str(min(limit * 3, 100)),
                "$select": _MS_SELECT,
            }
            items = self._request("GET", "/me/messages", params=params).json()
            messages = [self._summary(i) for i in items.get("value", [])]
            if unread_only:
                messages = [m for m in messages if m["unread"]]
            if cutoff:
                messages = [m for m in messages if m["date"] >= cutoff]
            messages.sort(key=lambda m: m["date"], reverse=True)
            return messages[:limit]
        filters = []
        if cutoff:
            filters.append(f"receivedDateTime ge {_iso(cutoff)}")
        if unread_only:
            filters.append("isRead eq false")
        params = {
            "$top": str(limit),
            "$select": _MS_SELECT,
            "$orderby": "receivedDateTime desc",
        }
        if filters:
            params["$filter"] = " and ".join(filters)
        items = self._request(
            "GET", "/me/mailFolders/inbox/messages", params=params
        ).json()
        return [self._summary(i) for i in items.get("value", [])]

    def read(self, message_id: str) -> Dict[str, Any]:
        item = self._request(
            "GET",
            f"/me/messages/{quote(message_id, safe='')}",
            params={
                "$select": _MS_SELECT + ",toRecipients,body",
            },
            headers={"Prefer": 'outlook.body-content-type="text"'},
        ).json()
        entry = self._summary(item)
        entry["to"] = ", ".join(
            (r.get("emailAddress") or {}).get("address", "")
            for r in item.get("toRecipients", [])
        )
        body = (item.get("body") or {}).get("content", "")
        if (item.get("body") or {}).get("contentType") == "html":
            body = html_to_text(body)
        entry["body"] = body.strip()
        return entry

    def send(
        self, to: str, subject: str, body: str, reply_to_id: str = ""
    ) -> Dict[str, Any]:
        try:
            if reply_to_id:
                self._request(
                    "POST",
                    f"/me/messages/{quote(reply_to_id, safe='')}/reply",
                    json={"comment": body},
                )
            else:
                self._request(
                    "POST",
                    "/me/sendMail",
                    json={
                        "message": {
                            "subject": subject or "(sin asunto)",
                            "body": {"contentType": "Text", "content": body},
                            "toRecipients": [
                                {"emailAddress": {"address": a.strip()}}
                                for a in to.split(",")
                                if a.strip()
                            ],
                        },
                        "saveToSentItems": True,
                    },
                )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "error": ""}

    def poll_new(
        self, state: Dict[str, Any], important_only: bool
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not state or not state.get("since"):
            return [], {"since": _iso(time.time())}
        since = state["since"]
        params = {
            "$filter": f"receivedDateTime gt {since}",
            "$orderby": "receivedDateTime asc",
            "$top": "50",
            "$select": _MS_SELECT,
        }
        items = (
            self._request("GET", "/me/mailFolders/inbox/messages", params=params)
            .json()
            .get("value", [])
        )
        if items:
            since = max(i.get("receivedDateTime", since) for i in items)
        if important_only:
            items = [i for i in items if i.get("inferenceClassification") != "other"]
        return [self._summary(i) for i in items[-_MAX_NEW_PER_POLL:]], {"since": since}


__all__ = [
    "GmailAccount",
    "MicrosoftAuth",
    "OutlookAccount",
    "html_to_text",
]
