"""Tests for plans, the AI credit meter and the related limits/endpoints."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.fasttrack import credits as credits_mod
from openjarvis.fasttrack import plans
from openjarvis.fasttrack.credits import CreditMeter


def test_credits_for_uses_real_prices(monkeypatch):
    monkeypatch.setenv("USD_EUR", "1")
    # Haiku 5.5: $0.10 / $0.50 per 1M -> 20k in + 1.5k out = $0.00275 = 0.275 credits
    assert plans.credits_for("claude-haiku-5-5", 20_000, 1_500) == pytest.approx(0.275)
    assert plans.credits_for("claude-sonnet-5-5", 20_000, 1_500) == pytest.approx(5.5)
    # Unknown models are priced conservatively, never for free.
    assert plans.credits_for("mystery-model", 1_000_000, 0) == pytest.approx(200)


def _meter(tmp_path, plan, notes):
    return CreditMeter(
        tmp_path / "credits.json",
        plan_name=plan,
        notifier=lambda t: notes.append(t) or True,
    )


class TestMeter:
    def test_own_plan_only_counts(self, tmp_path):
        notes = []
        m = _meter(tmp_path, "own", notes)
        m.record("claude-sonnet-5-5", 10_000_000, 0)
        st = m.status()
        assert st["lifetime"] > 0 and not st["exhausted"] and notes == []
        assert m.blocked_reply() is None

    def test_esencial_warns_then_pauses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USD_EUR", "1")
        notes = []
        m = _meter(tmp_path, "esencial", notes)  # 400 credits = 4 €
        m.record("claude-haiku-5-5", 33_000_000, 0)  # 330 credits -> 82 %
        assert len(notes) == 1 and "80 %" in notes[0]
        assert m.blocked_reply() is None
        m.record("claude-haiku-5-5", 10_000_000, 0)  # 430 -> exhausted
        assert len(notes) == 2 and "agotado" in notes[1]
        assert "créditos" in m.blocked_reply()
        st = m.add_topup(plans.TOPUP_CREDITS)
        assert not st["exhausted"] and m.blocked_reply() is None

    def test_pro_downgrades_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USD_EUR", "1")
        system = SimpleNamespace(model="claude-sonnet-5-5")
        m = _meter(tmp_path, "pro", [])
        m.attach(system=system)
        m.record("claude-sonnet-5-5", 1_000_000, 1_600_000)  # 200 + 1600 credits
        assert system.model == "claude-haiku-5-5"
        assert m.blocked_reply() is None  # Pro never pauses
        m.add_topup(1000)
        assert system.model == "claude-sonnet-5-5"

    def test_month_rollover_keeps_unused_topup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USD_EUR", "1")
        m = _meter(tmp_path, "esencial", [])
        m.add_topup(300)
        m.record("claude-haiku-5-5", 50_000_000, 0)  # 500 used: 400 monthly + 100 topup
        m._state["period"] = "2000-01"
        st = m.status()
        assert st["used"] == 0 and st["topup"] == 200

    def test_persisted(self, tmp_path):
        m = _meter(tmp_path, "pro", [])
        m.record("claude-sonnet-5-5", 100_000, 0)
        again = _meter(tmp_path, "pro", [])
        assert again.status()["used"] == m.status()["used"]

    def test_model_change_cooldown(self, tmp_path):
        m = _meter(tmp_path, "pro", [])
        assert m.can_change_model()
        m.mark_model_changed()
        assert not m.can_change_model()
        m._state["model_changed_at"] = time.time() - 25 * 3600
        assert m.can_change_model()
        own = _meter(tmp_path / "o", "own", [])
        own.mark_model_changed()
        assert own.can_change_model()  # unlimited with your own AI

    def test_bus_events_are_metered(self, tmp_path):
        from openjarvis.core.events import EventBus, EventType

        bus = EventBus()
        m = _meter(tmp_path, "pro", [])
        m.attach(bus=bus)
        bus.publish(
            EventType.INFERENCE_END,
            {
                "model": "claude-sonnet-5-5",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
            },
        )
        assert m.status()["used"] > 0


# ---------------------------------------------------------------------------
# Scheduled task limits
# ---------------------------------------------------------------------------


class TestTaskLimits:
    @pytest.fixture
    def svc(self, tmp_path):
        from openjarvis.personal.reminders import ReminderService
        from openjarvis.personal.store import PersonalStore

        store = PersonalStore(tmp_path / "p.db")
        yield ReminderService(store, notifier=lambda t: True)
        store.close()

    def test_too_frequent_rejected(self, svc):
        with pytest.raises(ValueError, match="30 minutos"):
            svc.create("x", "2030-03-10 09:00", repeat="*/10 * * * *", is_task=True)
        svc.create("x", "2030-03-10 09:00", repeat="*/30 * * * *", is_task=True)

    def test_max_tasks(self, svc):
        for i in range(10):
            svc.create(f"t{i}", "2030-03-10 09:00", repeat="daily", is_task=True)
        with pytest.raises(ValueError, match="10 tareas"):
            svc.create("t10", "2030-03-10 09:00", repeat="daily", is_task=True)
        svc.create("plain reminder", "2030-03-10 09:00")  # reminders are separate

    def test_paused_plan_skips_tasks(self, svc, tmp_path):
        meter = MagicMock()
        meter.blocked_reply.return_value = "sin créditos"
        sent = []
        svc._notifier = sent.append
        svc._task_runner = MagicMock(return_value="hecho")
        svc.create("resume", "2030-03-10 08:00", is_task=True)
        with patch.object(credits_mod, "get_meter", return_value=meter):
            svc.run_due(time.mktime((2030, 3, 10, 9, 0, 0, 0, 0, -1)))
        assert "sin créditos" in sent[0] and not svc._task_runner.called


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

KEY = "oj_sk_cr"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from openjarvis.server import ai_routes
    from openjarvis.server.app import create_app

    monkeypatch.setattr(ai_routes, "get_config_dir", lambda: tmp_path)
    monkeypatch.setenv("JARVIS_PLAN", "pro")
    monkeypatch.setenv("JARVIS_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shared")
    monkeypatch.setenv("FASTTRACK_ADMIN_TOKEN", "admintok")
    meter = CreditMeter(tmp_path / "credits.json", notifier=lambda t: True)
    monkeypatch.setattr(credits_mod, "_meter", meter)
    monkeypatch.setattr(ai_routes, "restart_server", lambda: True)
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["m"]
    return TestClient(create_app(engine, "m", api_key=KEY))


class TestEndpoints:
    def test_status_has_plan_and_choices(self, client):
        data = client.get("/api/ai/status", headers=AUTH).json()
        assert data["credits"]["plan"] == "pro" and data["credits"]["monthly"] == 1500
        assert {m["id"] for m in data["model_choices"]} >= {
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
        }

    def test_model_change_once_a_day(self, client):
        r = client.post(
            "/api/ai/model", json={"model": "claude-haiku-5-5"}, headers=AUTH
        )
        assert r.status_code == 200
        r = client.post(
            "/api/ai/model", json={"model": "claude-sonnet-5-5"}, headers=AUTH
        )
        assert r.status_code == 429 and "una vez al día" in r.json()["detail"]

    def test_model_outside_plan_rejected(self, client):
        r = client.post(
            "/api/ai/model", json={"model": "claude-opus-5-5"}, headers=AUTH
        )
        assert r.status_code == 400

    def test_topup_requires_admin_token(self, client):
        r = client.post("/api/credits/topup", json={"credits": 300}, headers=AUTH)
        assert r.status_code == 403
        r = client.post(
            "/api/credits/topup",
            json={"credits": 300},
            headers={**AUTH, "X-FastTrack-Admin": "admintok"},
        )
        assert r.status_code == 200 and r.json()["topup"] == 300
