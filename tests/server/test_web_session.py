"""Tests for browser sessions, the restricted page and brute-force limiting."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from openjarvis.server.web_session import (  # noqa: E402
    SESSION_COOKIE,
    FailureLimiter,
    client_ip,
    issue_session,
    session_valid,
)

KEY = "oj_sk_test"


def _request(headers=None, host="10.0.0.1"):
    return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=host))


class TestHelpers:
    def test_session_roundtrip(self):
        token = issue_session(KEY)
        assert session_valid(token, KEY)
        assert not session_valid(token, "oj_sk_rotated")  # rotation logs out
        assert not session_valid(token + "x", KEY)
        assert not session_valid("", KEY)
        expired = issue_session(KEY, now=time.time() - 40 * 24 * 3600)
        assert not session_valid(expired, KEY)

    def test_client_ip(self):
        assert client_ip(_request({"x-real-ip": "1.1.1.1"})) == "1.1.1.1"
        # The first hop is client-controlled; the proxy appends the real one.
        assert client_ip(_request({"x-forwarded-for": "6.6.6.6, 2.2.2.2"})) == "2.2.2.2"
        assert client_ip(_request()) == "10.0.0.1"

    def test_limiter(self):
        lim = FailureLimiter(max_failures=3, window_s=60, block_s=60)
        for _ in range(2):
            lim.fail("a")
        assert not lim.blocked("a")
        lim.fail("a")
        assert lim.blocked("a") and not lim.blocked("b")
        lim._blocked["a"] = time.time() - 1
        assert not lim.blocked("a")


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    from openjarvis.server.app import create_app

    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["m"]
    return TestClient(create_app(engine, "m", api_key=KEY))


class TestGate:
    def test_without_session_only_restricted_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Acceso restringido" in resp.text
        assert "Conecta tu WhatsApp" not in resp.text
        assert resp.headers["cache-control"] == "no-store"

    def test_bad_session_cookie(self, client):
        client.cookies.set(SESSION_COOKIE, "123.forged")
        assert "Acceso restringido" in client.get("/").text
        assert client.get("/api/jarvis/info").status_code == 401

    def test_key_exchange_then_cookie_works(self, client):
        assert client.post("/api/session").status_code == 401
        resp = client.post("/api/session", headers={"Authorization": f"Bearer {KEY}"})
        assert resp.status_code == 204
        cookie = resp.headers["set-cookie"]
        assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
        # The browser now gets the app and can call the API with the cookie.
        assert "Conecta tu WhatsApp" in client.get("/").text
        assert client.get("/api/jarvis/info").status_code == 200

    def test_cookie_writes_need_csrf_header(self, client):
        client.post("/api/session", headers={"Authorization": f"Bearer {KEY}"})
        assert client.post("/api/whatsapp/logout").status_code == 403
        resp = client.post(
            "/api/whatsapp/logout", headers={"X-Requested-With": "jarvis"}
        )
        assert resp.status_code == 404  # authorised; WhatsApp just isn't enabled

    def test_sign_out(self, client):
        client.post("/api/session", headers={"Authorization": f"Bearer {KEY}"})
        client.delete("/api/session", headers={"X-Requested-With": "jarvis"})
        assert "Acceso restringido" in client.get("/").text

    def test_brute_force_blocked(self, client):
        headers = {"Authorization": "Bearer wrong", "X-Real-IP": "9.9.9.9"}
        codes = [
            client.get("/api/jarvis/info", headers=headers).status_code
            for _ in range(20)
        ]
        assert set(codes) == {401}
        assert client.get("/api/jarvis/info", headers=headers).status_code == 429
        # Even the right key is refused from the blocked IP for a while...
        ok = {"Authorization": f"Bearer {KEY}", "X-Real-IP": "9.9.9.9"}
        assert client.get("/api/jarvis/info", headers=ok).status_code == 429
        # ...while other clients are unaffected.
        ok["X-Real-IP"] = "8.8.8.8"
        assert client.get("/api/jarvis/info", headers=ok).status_code == 200
