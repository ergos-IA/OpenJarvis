"""Tests for the customer email templates and the one-time access email."""

from __future__ import annotations

import pytest

from openjarvis.fasttrack import emails


def test_welcome_announces_second_email():
    subject, text, html = emails.welcome("<Eva> Pérez", "Esencial", assisted=True)
    assert "Bienvenido" in subject
    assert "segundo email" in text and "alta asistida" in text
    assert "&lt;Eva&gt;" in html and "<Eva>" not in html  # names are escaped
    assert "Esencial" in html and "<table" in html


def test_access_ready_has_code_link_and_steps():
    subject, text, html = emails.access_ready(
        "Eva",
        code="ABCD-EFGH-JKMN",
        link="https://x.up.railway.app/#key=k",
        assistant="Friday",
        own_ai=True,
    )
    assert subject.startswith("Friday está listo")
    assert "ABCD-EFGH-JKMN" in html and "ABCD-EFGH-JKMN" in text
    assert "https://x.up.railway.app/#key=k" in html
    assert "Conecta tu cuenta de IA" in html
    assert (
        "TU ENLACE DIRECTO" in html
        and html.count("https://x.up.railway.app/#key=k") >= 3
    )


def test_billing_notices():
    assert "cancelado" in emails.cancelled("Ana")[1]
    subject, text, html = emails.payment_failed("Ana", "https://invoice.stripe.com/i/x")
    assert "https://invoice.stripe.com/i/x" in html and "cobrar" in subject


def test_payload_shape(monkeypatch):
    monkeypatch.setenv("FROM_EMAIL", "no-reply@example.com")
    msg = emails.payload("eva@example.com", "Eva", emails.cancelled("Eva"))["Messages"][
        0
    ]
    assert msg["From"]["Email"] == "no-reply@example.com"
    assert msg["To"] == [{"Email": "eva@example.com", "Name": "Eva"}]
    assert msg["HTMLPart"].startswith("<!doctype html>")


class TestAccessEmailOnce:
    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SEND_ACCESS_EMAIL", "1")
        monkeypatch.setenv("JARVIS_USER_EMAIL", "eva@example.com")
        monkeypatch.setenv("JARVIS_USER_NAME", "Eva")
        monkeypatch.setenv("JARVIS_ACCESS_CODE", "ABCD-EFGH-JKMN")
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "jarvis-eva.up.railway.app")
        monkeypatch.setenv("OPENJARVIS_API_KEY", "oj_sk_k")
        monkeypatch.setenv("JARVIS_PLAN", "esencial")

    def test_sent_once(self, env, tmp_path):
        sent = []
        marker = tmp_path / "access_email_sent"
        assert emails.send_access_email_once(marker, send=sent.append)
        msg = sent[0]["Messages"][0]
        assert msg["To"][0]["Email"] == "eva@example.com"
        assert "https://jarvis-eva.up.railway.app/#key=oj_sk_k" in msg["HTMLPart"]
        assert "ya incluye la inteligencia artificial" in msg["HTMLPart"]
        assert not emails.send_access_email_once(marker, send=sent.append)
        assert len(sent) == 1

    def test_failure_retries_next_start(self, env, tmp_path):
        marker = tmp_path / "m"

        def boom(_):
            raise RuntimeError("mailjet down")

        assert not emails.send_access_email_once(marker, send=boom)
        assert not marker.exists()

    def test_only_for_self_service(self, env, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_SEND_ACCESS_EMAIL")
        assert not emails.send_access_email_once(tmp_path / "m", send=lambda m: None)
