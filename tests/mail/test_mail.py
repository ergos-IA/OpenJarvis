"""Tests for the mail package: Gmail/Outlook accounts, MailService, tools."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.channels import whatsapp_baileys as wb
from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
from openjarvis.channels.whatsapp_store import WhatsAppStore
from openjarvis.mail import service as mail_service
from openjarvis.mail.accounts import (
    GmailAccount,
    MicrosoftAuth,
    OutlookAccount,
    html_to_text,
)
from openjarvis.mail.service import MailService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_RAW = (
    b"From: =?utf-8?q?Pepe_P=C3=A9rez?= <pepe@example.com>\r\n"
    b"To: me@gmail.com\r\n"
    b"Subject: Factura octubre\r\n"
    b"Date: Mon, 26 Oct 2026 10:00:00 +0100\r\n"
    b"Message-ID: <abc@example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Hola, te adjunto la factura.\r\n"
)


class FakeImap:
    """Just enough of imaplib.IMAP4 for GmailAccount."""

    def __init__(self, inbox_uids=(5, 6), primary=(6,), validity=b"77"):
        self.inbox_uids = list(inbox_uids)
        self.primary = list(primary)
        self.validity = validity
        self.literal = None
        self.searches = []
        self.logged_in = None

    def login(self, user, password):
        if password != "secretpass":
            raise Exception("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged_in = user

    def logout(self):
        pass

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        return "OK", [b"2"]

    def response(self, code):
        return code, [self.validity]

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\All \\HasNoChildren) "/" "[Gmail]/Todos"',
        ]

    def uid(self, command, *args):
        if command == "SEARCH":
            if args[:1] == (None,):
                return "OK", [" ".join(map(str, self.inbox_uids)).encode()]
            if args[0] == "X-GM-MSGID":
                return "OK", [b"6"]
            self.searches.append(self.literal.decode())
            uids = self.primary if "category:primary" in self.searches[-1] else [6]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            out = []
            for uid in args[0].split(","):
                meta = f"1 (UID {uid} X-GM-MSGID 99{uid} FLAGS () BODY[] {{10}}"
                out.append((meta.encode(), _RAW))
                out.append(b")")
            return "OK", out
        raise AssertionError(command)


def _gmail(imap=None, smtp=None):
    imap = imap or FakeImap()
    return GmailAccount(
        "me@gmail.com",
        "secret pass",
        imap_factory=lambda: imap,
        smtp_factory=lambda: smtp or MagicMock(),
    ), imap


class FakeAccount:
    kind = "gmail"
    label = "Gmail"
    email = "me@gmail.com"

    def __init__(self, *args):
        self.new = []
        self.sent = []

    def verify(self):
        pass

    def poll_new(self, state, important_only):
        if not state:
            return [], {"pos": 1}
        new, self.new = self.new, []
        return new, {"pos": state["pos"] + len(new)}

    def list_messages(self, **kwargs):
        return [_msg()]

    def read(self, message_id):
        return {**_msg(), "body": "Hola, te adjunto la factura.", "to": self.email}

    def send(self, to, subject, body, reply_to_id=""):
        self.sent.append((to, subject, body, reply_to_id))
        return {"ok": True, "error": ""}


def _msg(**overrides):
    m = {
        "id": "991",
        "from_name": "Pepe Pérez",
        "from_addr": "pepe@example.com",
        "subject": "Factura octubre",
        "date": time.time(),
        "unread": True,
        "preview": "Hola, te adjunto la factura.",
    }
    m.update(overrides)
    return m


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


class TestGmail:
    def test_app_password_spaces_removed(self):
        account, imap = _gmail()
        account.verify()
        assert imap.logged_in == "me@gmail.com"

    def test_bad_password(self):
        account = GmailAccount("me@gmail.com", "wrong", imap_factory=FakeImap)
        with pytest.raises(Exception, match="AUTHENTICATIONFAILED"):
            account.verify()

    def test_list_parses_headers(self):
        account, imap = _gmail()
        [m] = account.list_messages(query="de Pepé", unread_only=True)
        assert imap.selected == '"[Gmail]/Todos"'
        assert imap.searches == ["de Pepé is:unread"]
        assert (m["id"], m["from_name"], m["from_addr"]) == (
            "996",
            "Pepe Pérez",
            "pepe@example.com",
        )
        assert m["subject"] == "Factura octubre"
        assert m["unread"] is True

    def test_read_body(self):
        account, _ = _gmail()
        m = account.read("996")
        assert m["body"] == "Hola, te adjunto la factura."
        assert m["message_id"] == "<abc@example.com>"

    def test_reply_sets_threading_headers(self):
        smtp = MagicMock()
        account, _ = _gmail(smtp=smtp)
        assert account.send("", "", "Gracias", reply_to_id="996")["ok"]
        sent = smtp.send_message.call_args[0][0]
        assert sent["To"] == "pepe@example.com"
        assert sent["Subject"] == "Re: Factura octubre"
        assert sent["In-Reply-To"] == "<abc@example.com>"
        smtp.login.assert_called_once_with("me@gmail.com", "secretpass")

    def test_send_failure_is_reported(self):
        smtp = MagicMock()
        smtp.send_message.side_effect = Exception("boom")
        account, _ = _gmail(smtp=smtp)
        assert account.send("a@b.com", "x", "y") == {"ok": False, "error": "boom"}

    def test_poll_first_run_only_records_position(self):
        account, _ = _gmail()
        new, state = account.poll_new({}, important_only=True)
        assert new == []
        assert state == {"uidvalidity": "77", "last_uid": 6}

    def test_poll_returns_only_new_primary(self):
        account, imap = _gmail(FakeImap(inbox_uids=(5, 6, 7), primary=(6,)))
        new, state = account.poll_new(
            {"uidvalidity": "77", "last_uid": 5}, important_only=True
        )
        assert [m["id"] for m in new] == ["996"]
        assert new[0]["preview"].startswith("Hola")
        assert state["last_uid"] == 7

    def test_poll_uidvalidity_change_resets(self):
        account, _ = _gmail()
        new, state = account.poll_new(
            {"uidvalidity": "1", "last_uid": 1}, important_only=False
        )
        assert new == [] and state["uidvalidity"] == "77"


def test_html_to_text():
    raw = "<html><style>p{}</style><p>Hola&nbsp;<b>Pepe</b></p><p>Adiós</p></html>"
    assert html_to_text(raw) == "Hola Pepe\nAdiós"


# ---------------------------------------------------------------------------
# Microsoft
# ---------------------------------------------------------------------------


def _ms(handler, tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = MicrosoftAuth("cid", "tid", tmp_path / "ms.json", http=http)
    return auth, http


def _token(**extra):
    return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, **extra}


class TestMicrosoftAuth:
    def test_device_flow(self, tmp_path, monkeypatch):
        go = threading.Event()
        # Hold the sign-in poller until the "pending" state has been checked.
        monkeypatch.setattr("openjarvis.mail.accounts.time.sleep", lambda s: go.wait(5))
        polls = []

        def handler(request: httpx.Request):
            if request.url.path.endswith("/devicecode"):
                assert b"Mail.Send" in request.content
                return httpx.Response(
                    200,
                    json={
                        "user_code": "ABCD123",
                        "verification_uri": "https://microsoft.com/devicelogin",
                        "device_code": "DC",
                        "interval": 1,
                        "expires_in": 900,
                    },
                )
            if request.url.path.endswith("/token"):
                polls.append(1)
                if len(polls) == 1:
                    return httpx.Response(400, json={"error": "authorization_pending"})
                return httpx.Response(200, json=_token())
            assert request.url.path == "/v1.0/me"
            return httpx.Response(200, json={"mail": "jose@edge.es"})

        auth, _ = _ms(handler, tmp_path)
        status = auth.start_device_flow()
        assert status["state"] == "pending" and status["user_code"] == "ABCD123"
        go.set()
        auth._flow_thread.join(5)
        assert auth.status() == {"state": "connected", "email": "jose@edge.es"}
        saved = json.loads((tmp_path / "ms.json").read_text())
        assert saved["refresh_token"] == "RT"
        # Reloaded from disk by a fresh instance.
        again, _ = _ms(handler, tmp_path)
        assert again.connected and again.email == "jose@edge.es"

    def test_declined(self, tmp_path, monkeypatch):
        monkeypatch.setattr("openjarvis.mail.accounts.time.sleep", lambda s: None)

        def handler(request):
            if request.url.path.endswith("/devicecode"):
                return httpx.Response(
                    200,
                    json={
                        "user_code": "X",
                        "verification_uri": "u",
                        "device_code": "DC",
                        "interval": 1,
                    },
                )
            return httpx.Response(
                400,
                json={"error": "access_denied", "error_description": "denied\r\nmore"},
            )

        auth, _ = _ms(handler, tmp_path)
        auth.start_device_flow()
        auth._flow_thread.join(5)
        assert auth.status()["state"] == "error"
        assert auth.status()["error"] == "denied"

    def test_refresh(self, tmp_path):
        (tmp_path / "ms.json").write_text(
            json.dumps({"refresh_token": "OLD", "access_token": "x", "expires_at": 0})
        )

        def handler(request):
            assert b"refresh_token=OLD" in request.content
            return httpx.Response(200, json=_token(access_token="NEW"))

        auth, _ = _ms(handler, tmp_path)
        assert auth.access_token() == "NEW"
        assert auth.access_token() == "NEW"  # cached now

    def test_revoked_refresh_disconnects(self, tmp_path):
        (tmp_path / "ms.json").write_text(json.dumps({"refresh_token": "OLD"}))
        auth, _ = _ms(
            lambda r: httpx.Response(400, json={"error": "invalid_grant"}), tmp_path
        )
        with pytest.raises(RuntimeError):
            auth.access_token()
        assert not auth.connected
        assert not (tmp_path / "ms.json").exists()


def _outlook(handler, tmp_path):
    (tmp_path / "ms.json").write_text(
        json.dumps(
            {
                "refresh_token": "RT",
                "access_token": "AT",
                "expires_at": time.time() + 3600,
                "email": "jose@edge.es",
            }
        )
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = MicrosoftAuth("cid", "tid", tmp_path / "ms.json", http=http)
    return OutlookAccount(auth, http=http)


_GRAPH_ITEM = {
    "id": "AAMk/abc=",
    "subject": "Reunión",
    "from": {"emailAddress": {"name": "Ana", "address": "ana@cliente.com"}},
    "receivedDateTime": "2026-10-26T09:00:00Z",
    "isRead": False,
    "bodyPreview": "¿Podemos  vernos\r\nmañana?",
    "inferenceClassification": "focused",
}


class TestOutlook:
    def test_list_inbox_with_filters(self, tmp_path):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            assert request.headers["Authorization"] == "Bearer AT"
            return httpx.Response(200, json={"value": [_GRAPH_ITEM]})

        [m] = _outlook(handler, tmp_path).list_messages(unread_only=True, since_hours=2)
        assert seen["path"] == "/v1.0/me/mailFolders/inbox/messages"
        assert "isRead eq false" in seen["params"]["$filter"]
        assert m["from_name"] == "Ana" and m["unread"] is True
        assert m["preview"] == "¿Podemos vernos mañana?"

    def test_search(self, tmp_path):
        def handler(request):
            assert request.url.params["$search"] == '"factura Ana"'
            return httpx.Response(200, json={"value": [_GRAPH_ITEM]})

        assert (
            len(_outlook(handler, tmp_path).list_messages(query='factura "Ana"')) == 1
        )

    def test_read_quotes_id(self, tmp_path):
        def handler(request):
            assert request.url.raw_path.startswith(b"/v1.0/me/messages/AAMk%2Fabc%3D")
            return httpx.Response(
                200,
                json={
                    **_GRAPH_ITEM,
                    "toRecipients": [{"emailAddress": {"address": "jose@edge.es"}}],
                    "body": {"contentType": "text", "content": "Hola Jose\n"},
                },
            )

        m = _outlook(handler, tmp_path).read("AAMk/abc=")
        assert m["body"] == "Hola Jose" and m["to"] == "jose@edge.es"

    def test_send_and_reply(self, tmp_path):
        calls = []

        def handler(request):
            calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(202)

        account = _outlook(handler, tmp_path)
        assert account.send("a@b.com, c@d.com", "Hola", "Texto")["ok"]
        assert account.send("", "", "Vale", reply_to_id="AAMk")["ok"]
        path, payload = calls[0]
        assert path == "/v1.0/me/sendMail"
        assert len(payload["message"]["toRecipients"]) == 2
        assert calls[1] == ("/v1.0/me/messages/AAMk/reply", {"comment": "Vale"})

    def test_error_is_returned(self, tmp_path):
        account = _outlook(
            lambda r: httpx.Response(403, json={"error": {"message": "Denied"}}),
            tmp_path,
        )
        assert account.send("a@b.com", "x", "y") == {
            "ok": False,
            "error": "Outlook (403): Denied",
        }

    def test_poll(self, tmp_path):
        other = {
            **_GRAPH_ITEM,
            "id": "2",
            "inferenceClassification": "other",
            "receivedDateTime": "2026-10-26T10:00:00Z",
        }

        def handler(request):
            assert (
                "receivedDateTime gt 2026-10-26T08:00:00Z"
                in request.url.params["$filter"]
            )
            return httpx.Response(200, json={"value": [_GRAPH_ITEM, other]})

        account = _outlook(handler, tmp_path)
        assert account.poll_new({}, True)[0] == []
        new, state = account.poll_new({"since": "2026-10-26T08:00:00Z"}, True)
        assert [m["id"] for m in new] == ["AAMk/abc="]
        assert state == {"since": "2026-10-26T10:00:00Z"}


# ---------------------------------------------------------------------------
# MailService
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    notes = []
    s = MailService(
        tmp_path / "mail",
        env={},
        gmail_factory=FakeAccount,
        notifier=lambda text: notes.append(text) or True,
    )
    s.notes = notes
    return s


class TestService:
    def test_no_outlook_without_client_id(self, svc):
        assert svc.status()["outlook"] == {"state": "unavailable"}
        assert svc.accounts() == {}

    def test_outlook_configured(self, tmp_path):
        s = MailService(tmp_path, env={"MS_CLIENT_ID": "cid", "MS_TENANT_ID": "t"})
        assert s.status()["outlook"]["state"] == "disconnected"
        assert s.ms_auth.tenant == "t"

    def test_connect_gmail_persists(self, svc, tmp_path):
        svc.connect_gmail("me@gmail.com", "abcd efgh")
        saved = json.loads((tmp_path / "mail" / "gmail.json").read_text())
        assert saved == {"email": "me@gmail.com", "app_password": "abcdefgh"}
        again = MailService(tmp_path / "mail", env={}, gmail_factory=FakeAccount)
        assert again.status()["gmail"] == {
            "state": "connected",
            "email": "me@gmail.com",
        }
        svc.disconnect_gmail()
        assert svc.status()["gmail"]["state"] == "disconnected"

    def test_account_selection(self, svc):
        with pytest.raises(LookupError):
            svc.account()
        svc.connect_gmail("me@gmail.com", "x")
        assert svc.account().kind == "gmail"
        with pytest.raises(LookupError, match="outlook"):
            svc.account("outlook")

    def test_short_ids_are_stable(self, svc):
        a = svc.short_id("outlook", "LONG-ID")
        assert a == "o1"
        assert svc.short_id("outlook", "LONG-ID") == "o1"
        assert svc.short_id("gmail", "123") == "g2"
        assert svc.resolve_id("O1") == ("outlook", "LONG-ID")
        with pytest.raises(LookupError):
            svc.resolve_id("o99")

    def test_poll_notifies_new_mail(self, svc):
        svc.connect_gmail("me@gmail.com", "x")
        assert svc.poll_once() == 0  # first run records the position
        svc._gmail.new = [_msg()]
        assert svc.poll_once() == 1
        [note] = svc.notes
        assert note.startswith("📧 *Gmail* · Pepe Pérez <pepe@example.com>")
        assert "*Factura octubre*" in note and "(ref. g1)" in note
        assert svc.poll_once() == 0

    def test_poll_error_is_shown_in_status(self, svc):
        svc.connect_gmail("me@gmail.com", "x")
        svc._gmail.poll_new = MagicMock(side_effect=Exception("IMAP caído"))
        svc.poll_once()
        assert svc.status()["gmail"]["warning"] == "IMAP caído"

    def test_notify_none(self, tmp_path):
        s = MailService(
            tmp_path, env={"EMAIL_NOTIFY": "none"}, gmail_factory=FakeAccount
        )
        s.connect_gmail("me@gmail.com", "x")
        assert s.poll_once() == 0


# ---------------------------------------------------------------------------
# Tools + WhatsApp confirmation
# ---------------------------------------------------------------------------


@pytest.fixture
def channel(tmp_path):
    store = WhatsAppStore(tmp_path / "wa.db")
    ch = WhatsAppBaileysChannel(store=store)
    ch._process = MagicMock()
    ch._handle_bridge_event(
        {"type": "status", "status": "connected", "me": "34600000000"}
    )
    with patch.object(wb, "_active_channel", ch):
        yield ch
    store.close()


@pytest.fixture
def linked(svc):
    svc.connect_gmail("me@gmail.com", "x")
    with patch.object(mail_service, "_service", svc):
        yield svc


class TestTools:
    def test_registered(self):
        import importlib

        import openjarvis.tools.email_tools as et
        from openjarvis.core.registry import ToolRegistry

        importlib.reload(et)
        for name in ("email_list", "email_read", "email_send"):
            assert ToolRegistry.contains(name)

    def test_list(self, linked):
        from openjarvis.tools.email_tools import EmailListTool

        res = EmailListTool().execute(unread_only=True)
        assert res.success
        assert "Gmail (me@gmail.com): 1" in res.content
        assert "[g1]" in res.content and "NO LEÍDO" in res.content

    def test_list_no_accounts(self, svc):
        from openjarvis.tools.email_tools import EmailListTool

        with patch.object(mail_service, "_service", svc):
            assert EmailListTool().execute().success is False

    def test_read(self, linked):
        from openjarvis.tools.email_tools import EmailReadTool

        ref = linked.short_id("gmail", "991")
        res = EmailReadTool().execute(ref=ref)
        assert res.success and "Hola, te adjunto la factura." in res.content
        assert EmailReadTool().execute(ref="g42").success is False

    def test_send_needs_confirmation(self, linked, channel):
        from openjarvis.tools.email_tools import EmailSendTool

        res = EmailSendTool().execute(to="ana@b.com", subject="Hola", body="Qué tal")
        assert res.success and "TODAVÍA NO ESTÁ ENVIADO" in res.content
        assert linked._gmail.sent == []
        reply = channel._take_pending_reply("¡Sí!")
        assert reply == "✅ Enviado a ana@b.com (correo Gmail)."
        assert linked._gmail.sent == [("ana@b.com", "Hola", "Qué tal", "")]

    def test_reply_uses_original_sender(self, linked, channel):
        from openjarvis.tools.email_tools import EmailSendTool

        ref = linked.short_id("gmail", "991")
        res = EmailSendTool().execute(reply_to=ref, body="Recibida, gracias")
        assert "pepe@example.com" in res.content
        assert channel._take_pending_reply("no") == "❌ Cancelado, no he enviado nada."
        assert linked._gmail.sent == []

    def test_send_rejects_names(self, linked, channel):
        from openjarvis.tools.email_tools import EmailSendTool

        res = EmailSendTool().execute(to="Ana", body="x")
        assert res.success is False and channel._pending is None

    def test_send_requires_whatsapp(self, linked):
        from openjarvis.tools.email_tools import EmailSendTool

        with patch.object(wb, "_active_channel", None):
            res = EmailSendTool().execute(to="a@b.com", body="x")
        assert res.success is False and "WhatsApp" in res.content

    def test_failed_action_reported(self, channel):
        channel.propose_action("X", lambda: {"ok": False, "error": "SMTP"})
        assert channel._take_pending_reply("si") == "⚠️ No se pudo enviar a X: SMTP"
        channel.propose_action("X", MagicMock(side_effect=Exception("boom")))
        assert "boom" in channel._take_pending_reply("si")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestRoutes:
    @pytest.fixture
    def client(self, svc):
        pytest.importorskip("fastapi")
        from starlette.testclient import TestClient

        from openjarvis.server.app import create_app

        engine = MagicMock()
        engine.engine_id = "mock"
        engine.health.return_value = True
        engine.list_models.return_value = ["m"]
        with patch.object(mail_service, "_service", svc):
            yield TestClient(create_app(engine, "m", api_key="k"))

    def test_page_is_public(self, client):
        r = client.get("/correo")
        assert r.status_code == 200 and "Conectar correo" in r.text

    def test_api_needs_key(self, client):
        assert client.get("/api/mail/status").status_code == 401

    def test_link_gmail(self, client, svc):
        h = {"Authorization": "Bearer k"}
        r = client.post(
            "/api/mail/gmail",
            json={"email": "me@gmail.com", "app_password": "x"},
            headers=h,
        )
        assert r.status_code == 200 and r.json()["gmail"]["state"] == "connected"
        svc._gmail_factory = MagicMock(
            return_value=MagicMock(verify=MagicMock(side_effect=Exception("bad")))
        )
        r = client.post(
            "/api/mail/gmail",
            json={"email": "me@gmail.com", "app_password": "y"},
            headers=h,
        )
        assert r.status_code == 400 and "bad" in r.json()["detail"]

    def test_outlook_unconfigured(self, client):
        r = client.post(
            "/api/mail/outlook/start", headers={"Authorization": "Bearer k"}
        )
        assert r.status_code == 404
