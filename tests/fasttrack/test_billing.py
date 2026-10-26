"""Tests for Stripe self-service sign-up, top-ups and webhook fulfilment."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

import httpx
import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from starlette.testclient import TestClient  # noqa: E402

from openjarvis.fasttrack import billing  # noqa: E402
from openjarvis.fasttrack.landing import create_landing_app  # noqa: E402

KEY = "oj_sk_" + "a" * 43


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256)
    return f"t={ts},v1={sig.hexdigest()}"


class FakeStripe:
    """httpx transport that records Stripe calls and answers plausibly."""

    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/v1/", "")
        if request.method == "GET":
            data = dict(request.url.params.multi_items())
        else:
            data = dict(parse_qsl(request.content.decode()))
        self.calls.append((request.method, path, data))
        if path == "prices":
            key = data["lookup_keys[0]"]
            return httpx.Response(200, json={"data": [{"id": "price_" + key}]})
        if path == "checkout/sessions":
            return httpx.Response(200, json={"url": "https://checkout.stripe.com/c/x"})
        if path == "billing_portal/sessions":
            return httpx.Response(200, json={"url": "https://billing.stripe.com/p/x"})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    def client(self) -> billing.StripeClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return billing.StripeClient("sk_test_x", http=http)

    def sessions(self):
        return [c[2] for c in self.calls if c[1] == "checkout/sessions"]


def test_form_encoding_nests_like_stripe():
    items = billing._form(
        {"line_items": [{"price": "p", "quantity": 1}], "a": {"b": True}, "n": None}
    )
    assert items == [
        ("line_items[0][price]", "p"),
        ("line_items[0][quantity]", "1"),
        ("a[b]", "true"),
    ]


class TestSignature:
    def test_valid(self):
        body = json.dumps({"id": "evt_1"}).encode()
        assert (
            billing.verify_signature(body, _sign(body, "whsec"), "whsec")["id"]
            == "evt_1"
        )

    @pytest.mark.parametrize(
        "header",
        ["", "t=1", "t=abc,v1=00", "v1=00"],
    )
    def test_malformed(self, header):
        with pytest.raises(billing.StripeError):
            billing.verify_signature(b"{}", header, "whsec")

    def test_wrong_secret_or_body(self):
        body = b'{"id":"evt"}'
        with pytest.raises(billing.StripeError):
            billing.verify_signature(body, _sign(body, "other"), "whsec")
        with pytest.raises(billing.StripeError):
            billing.verify_signature(b'{"id":"x"}', _sign(body, "whsec"), "whsec")

    def test_too_old(self):
        body = b"{}"
        old = int(time.time()) - 3600
        with pytest.raises(billing.StripeError, match="caducado"):
            billing.verify_signature(body, _sign(body, "whsec", old), "whsec")


class TestSessions:
    def test_signup_with_trial_tax_and_assisted(self):
        fake = FakeStripe()
        url = asyncio.run(
            billing.signup_session(
                fake.client(), plan="pro", interval="year", assisted=True
            )
        )
        assert url.startswith("https://checkout.stripe.com")
        s = fake.sessions()[0]
        assert s["mode"] == "subscription"
        assert s["line_items[0][price]"] == "price_jarvis_pro_year"
        assert s["line_items[1][price]"] == "price_jarvis_setup"
        assert s["subscription_data[trial_period_days]"] == "30"
        assert s["automatic_tax[enabled]"] == "true"
        assert s["metadata[plan]"] == "pro" and s["metadata[assisted]"] == "1"

    def test_signup_rejects_unknown(self):
        with pytest.raises(ValueError):
            asyncio.run(
                billing.signup_session(
                    FakeStripe().client(), plan="x", interval="month"
                )
            )
        with pytest.raises(ValueError):
            asyncio.run(
                billing.signup_session(
                    FakeStripe().client(), plan="pro", interval="week"
                )
            )

    def test_prices_are_cached(self):
        fake = FakeStripe()
        client = fake.client()
        for _ in range(2):
            asyncio.run(billing.signup_session(client, plan="own", interval="month"))
        assert sum(1 for c in fake.calls if c[1] == "prices") == 1

    def test_topup_session(self):
        fake = FakeStripe()
        asyncio.run(
            billing.topup_session(fake.client(), service_id="S1", customer="cus_1")
        )
        s = fake.sessions()[0]
        assert s["mode"] == "payment" and s["customer"] == "cus_1"
        assert s["metadata[kind]"] == "topup" and s["metadata[service_id]"] == "S1"
        assert s["line_items[0][price]"] == "price_jarvis_topup"


# ---------------------------------------------------------------------------
# Public site routes
# ---------------------------------------------------------------------------


class FakeRailway:
    configured = True

    def __init__(self, plan="esencial", customer="cus_1"):
        self.services = [{"id": "S1", "name": "jarvis-ana"}]
        self.vars = {
            "S1": {
                "OPENJARVIS_API_KEY": KEY,
                "JARVIS_ACCESS_CODE": "ABCD-EFGH-JKMN",
                "JARVIS_PLAN": plan,
                "STRIPE_CUSTOMER_ID": customer,
                "JARVIS_USER_NAME": "Ana",
                "JARVIS_USER_EMAIL": "ana@example.com",
            }
        }

    def project_overview(self):
        return {"services": self.services}

    def variables(self, sid):
        return self.vars.get(sid, {})

    def domain(self, sid):
        return f"{sid.lower()}.up.railway.app"


class RecordingFulfillment:
    def __init__(self):
        self.events = []
        self.seen = set()

    def first_time(self, event_id):
        new = event_id not in self.seen
        self.seen.add(event_id)
        return new

    async def handle(self, event):
        self.events.append(event)


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec")

    def make(rw=None):
        fake = FakeStripe()
        ful = RecordingFulfillment()
        app = create_landing_app(
            rw or FakeRailway(), stripe=fake.client(), fulfillment=ful
        )
        return TestClient(app), fake, ful

    return make


class TestRoutes:
    def test_signup_returns_checkout_url(self, site):
        client, fake, _ = site()
        r = client.post("/api/alta", json={"plan": "esencial", "interval": "month"})
        assert r.status_code == 200 and r.json()["url"].startswith("https://checkout")
        r = client.post("/api/alta", json={"plan": "gold"})
        assert r.status_code == 400

    def test_signup_without_stripe(self, site, monkeypatch):
        client, _, _ = site()
        monkeypatch.delenv("STRIPE_SECRET_KEY")
        assert client.post("/api/alta", json={"plan": "pro"}).status_code == 503

    def test_portal_by_access_code(self, site):
        client, fake, _ = site()
        r = client.post("/api/cuenta/portal", json={"code": "abcd efgh jkmn"})
        assert r.status_code == 200 and "billing.stripe.com" in r.json()["url"]
        portal = [c for c in fake.calls if c[1] == "billing_portal/sessions"][0][2]
        assert portal["customer"] == "cus_1"
        r = client.post("/api/cuenta/portal", json={"code": "ZZZZ-ZZZZ-ZZZZ"})
        assert r.status_code == 404

    def test_portal_needs_online_subscription(self, site):
        client, _, _ = site(FakeRailway(customer=""))
        r = client.post("/api/cuenta/portal", json={"code": "ABCD-EFGH-JKMN"})
        assert r.status_code == 409

    def test_topup_only_with_included_ai(self, site):
        client, fake, _ = site()
        r = client.post("/api/cuenta/recarga", json={"code": "ABCD-EFGH-JKMN"})
        assert r.status_code == 200
        assert fake.sessions()[0]["metadata[service_id]"] == "S1"
        client, _, _ = site(FakeRailway(plan="own"))
        r = client.post("/api/cuenta/recarga", json={"code": "ABCD-EFGH-JKMN"})
        assert r.status_code == 409

    def test_webhook_signature_and_dedupe(self, site):
        client, _, ful = site()
        body = json.dumps({"id": "evt_1", "type": "x"}).encode()
        r = client.post(
            "/api/stripe/webhook",
            content=body,
            headers={"stripe-signature": "t=1,v1=0"},
        )
        assert r.status_code == 400 and not ful.events
        for _ in range(2):
            r = client.post(
                "/api/stripe/webhook",
                content=body,
                headers={"stripe-signature": _sign(body, "whsec")},
            )
            assert r.status_code == 200
        assert [e["id"] for e in ful.events] == ["evt_1"]

    def test_account_page(self, site):
        client, _, _ = site()
        r = client.get("/cuenta")
        assert r.status_code == 200 and "/api/cuenta/portal" in r.text


# ---------------------------------------------------------------------------
# Fulfilment
# ---------------------------------------------------------------------------


@pytest.fixture
def mailjet(monkeypatch):
    monkeypatch.setenv("MAILJET_API_KEY", "k")
    monkeypatch.setenv("MAILJET_SECRET_KEY", "s")
    monkeypatch.setenv("CONTACT_TO", "team@example.com")
    sent = []

    async def send(payload):
        sent.append(payload["Messages"][0])

    return sent, send


def _completed(meta, **extra):
    session = {
        "customer": "cus_9",
        "subscription": "sub_9",
        "customer_details": {"email": "eva@example.com", "name": "Eva Pérez"},
        "custom_fields": [{"key": "assistant", "text": {"value": "Friday"}}],
        "metadata": meta,
        **extra,
    }
    return {
        "id": "evt_x",
        "type": "checkout.session.completed",
        "data": {"object": session},
    }


class TestFulfillment:
    def test_signup_provisions_and_emails(self, monkeypatch, mailjet):
        sent, send = mailjet
        created = []

        def fake_provision(rw, **kw):
            created.append(kw)
            return {
                "service": "jarvis-eva-perez",
                "access_code": "QRST-UVWX-YZ23",
                "build_error": "",
            }

        monkeypatch.setattr(billing, "provision_user", fake_provision)
        ful = billing.Fulfillment(object(), lambda **kw: None, send=send)
        asyncio.run(
            ful.handle(_completed({"kind": "signup", "plan": "pro", "assisted": "1"}))
        )
        kw = created[0]
        assert kw["name"] == "Eva Pérez" and kw["email"] == "eva@example.com"
        assert kw["assistant_name"] == "Friday" and kw["shared_ai"] is True
        assert kw["model"] == "claude-sonnet-5-5"
        extra = kw["extra_vars"]
        assert extra["JARVIS_PLAN"] == "pro" and extra["STRIPE_CUSTOMER_ID"] == "cus_9"
        assert extra["STRIPE_SUBSCRIPTION_ID"] == "sub_9"
        # The instance sends the access email itself once it is up.
        assert extra["JARVIS_SEND_ACCESS_EMAIL"] == "1"
        assert extra["MAILJET_API_KEY"] == "${{shared.MAILJET_API_KEY}}"
        welcome, team = sent
        assert welcome["To"][0]["Email"] == "eva@example.com"
        assert welcome["Subject"].startswith("Bienvenido a Jarvis")
        # No access code yet: it comes in the second email.
        assert "QRST-UVWX-YZ23" not in welcome["TextPart"] + welcome["HTMLPart"]
        assert "segundo email" in welcome["TextPart"]
        assert "alta asistida" in welcome["TextPart"]
        assert "Pro" in welcome["HTMLPart"]
        assert team["To"][0]["Email"] == "team@example.com" and "SÍ" in team["TextPart"]

    def test_own_plan_uses_customers_ai(self, monkeypatch, mailjet):
        _, send = mailjet
        created = []
        monkeypatch.setattr(
            billing,
            "provision_user",
            lambda rw, **kw: (
                created.append(kw)
                or {"service": "s", "access_code": "C", "build_error": ""}
            ),
        )
        ful = billing.Fulfillment(object(), lambda **kw: None, send=send)
        asyncio.run(ful.handle(_completed({"kind": "signup", "plan": "own"})))
        assert created[0]["shared_ai"] is False

    def test_already_provisioned_is_skipped(self, monkeypatch, mailjet):
        sent, send = mailjet
        monkeypatch.setattr(
            billing, "provision_user", lambda *a, **k: pytest.fail("dup")
        )
        ful = billing.Fulfillment(object(), lambda **kw: {"id": "S1"}, send=send)
        asyncio.run(ful.handle(_completed({"kind": "signup", "plan": "pro"})))
        assert sent == []

    def test_failure_alerts_the_team(self, monkeypatch, mailjet):
        sent, send = mailjet

        def boom(*a, **k):
            raise RuntimeError("railway down")

        monkeypatch.setattr(billing, "provision_user", boom)
        ful = billing.Fulfillment(object(), lambda **kw: None, send=send)
        asyncio.run(ful.handle(_completed({"kind": "signup", "plan": "pro"})))
        assert "ERROR" in sent[0]["Subject"] and "railway down" in sent[0]["TextPart"]

    def test_topup_calls_instance(self, monkeypatch, mailjet):
        _, send = mailjet
        monkeypatch.setenv("FASTTRACK_ADMIN_TOKEN", "adm")
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ful = billing.Fulfillment(
            FakeRailway(), lambda **kw: None, send=send, http=http
        )
        asyncio.run(ful.handle(_completed({"kind": "topup", "service_id": "S1"})))
        req = seen[0]
        assert str(req.url) == "https://s1.up.railway.app/api/credits/topup"
        assert req.headers["X-FastTrack-Admin"] == "adm"
        assert req.headers["Authorization"] == f"Bearer {KEY}"
        assert json.loads(req.content) == {"credits": 300}

    def test_cancel_and_failed_payment_emails(self, mailjet):
        sent, send = mailjet
        entry = {"name": "Ana", "email": "ana@example.com", "service": "jarvis-ana"}
        ful = billing.Fulfillment(object(), lambda **kw: entry, send=send)
        asyncio.run(
            ful.handle(
                {
                    "id": "e1",
                    "type": "customer.subscription.deleted",
                    "data": {"object": {"id": "sub_1", "customer": "cus_1"}},
                }
            )
        )
        assert {m["To"][0]["Email"] for m in sent} == {
            "ana@example.com",
            "team@example.com",
        }
        sent.clear()
        asyncio.run(
            ful.handle(
                {
                    "id": "e2",
                    "type": "invoice.payment_failed",
                    "data": {
                        "object": {
                            "id": "in_1",
                            "customer_email": "ana@example.com",
                            "hosted_invoice_url": "https://invoice.stripe.com/i/x",
                        }
                    },
                }
            )
        )
        assert "https://invoice.stripe.com/i/x" in sent[0]["TextPart"]

    def test_event_dedupe(self):
        ful = billing.Fulfillment(object(), lambda **kw: None)
        assert ful.first_time("evt_a") and not ful.first_time("evt_a")
