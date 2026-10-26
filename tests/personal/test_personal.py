"""Tests for reminders, scheduled tasks and remembered facts."""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from openjarvis.personal import reminders as rem
from openjarvis.personal.reminders import (
    ReminderService,
    describe_cron,
    parse_when,
    repeat_to_cron,
)
from openjarvis.personal.store import MAX_FACTS, PersonalStore

MADRID = ZoneInfo("Europe/Madrid")


@pytest.fixture(autouse=True)
def madrid(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Madrid")


@pytest.fixture
def store(tmp_path):
    s = PersonalStore(tmp_path / "p.db")
    yield s
    s.close()


@pytest.fixture
def svc(store):
    sent = []
    s = ReminderService(store, notifier=lambda t: sent.append(t) or True)
    s.sent = sent
    return s


def _ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=MADRID).timestamp()


class TestParsing:
    def test_parse_local_time(self):
        assert parse_when("2030-03-10 09:30") == _ts("2030-03-10 09:30")
        assert parse_when("2030-03-10T09:30") == _ts("2030-03-10 09:30")

    def test_parse_invalid(self):
        with pytest.raises(ValueError, match="AAAA-MM-DD"):
            parse_when("mañana")

    @pytest.mark.parametrize(
        "repeat,cron",
        [
            ("", ""),
            ("daily", "30 9 * * *"),
            ("weekdays", "30 9 * * 1-5"),
            ("weekly", "30 9 * * 0"),  # 2030-03-10 is a Sunday
            ("monthly", "30 9 10 * *"),
            ("0 8 * * 1", "0 8 * * 1"),
        ],
    )
    def test_repeat_to_cron(self, repeat, cron):
        assert repeat_to_cron(repeat, _ts("2030-03-10 09:30")) == cron

    def test_bad_repeat(self):
        with pytest.raises(ValueError):
            repeat_to_cron("a veces", time.time())

    def test_describe(self):
        assert describe_cron("30 9 * * 1-5") == "de lunes a viernes a las 09:30"
        assert describe_cron("0 8 * * 1") == "cada lunes a las 08:00"
        assert describe_cron("0 8 * * 0") == "cada domingo a las 08:00"


class TestReminders:
    def test_one_off(self, svc, store):
        item = svc.create("llamar a Pepe", "2030-03-10 09:30")
        assert svc.run_due(_ts("2030-03-10 09:29")) == 0
        assert svc.run_due(_ts("2030-03-10 09:30")) == 1
        assert svc.sent == ["⏰ *Recordatorio*: llamar a Pepe"]
        assert store.get_reminder(item["id"])["status"] == "done"
        assert svc.run_due(_ts("2030-03-10 10:00")) == 0

    def test_past_time_rejected(self, svc):
        with pytest.raises(ValueError, match="ya ha pasado"):
            svc.create("x", "2001-01-01 10:00")

    def test_recurring_moves_forward(self, svc, store):
        item = svc.create("pastilla", "2030-03-10 09:30", repeat="daily")
        svc.run_due(_ts("2030-03-10 09:30"))
        assert store.get_reminder(item["id"])["next_run"] == _ts("2030-03-11 09:30")
        # After downtime the next run is in the future, not a burst of repeats.
        svc.run_due(_ts("2030-03-14 12:00"))
        assert store.get_reminder(item["id"])["next_run"] == _ts("2030-03-15 09:30")
        assert "era para el" in svc.sent[-1]

    def test_not_delivered_without_whatsapp(self, store):
        s = ReminderService(store, notifier=lambda t: False)
        item = s.create("x", "2030-03-10 09:30")
        assert s.run_due(_ts("2030-03-10 09:31")) == 0
        assert store.get_reminder(item["id"])["status"] == "active"

    def test_cancel_and_list(self, svc):
        a = svc.create("a", "2030-03-10 09:30")
        svc.create("b", "2030-03-11 09:30")
        assert svc.cancel(a["id"]) is True
        assert svc.cancel(a["id"]) is False
        assert [i["text"] for i in svc.active()] == ["b"]

    def test_task_runs_prompt(self, store):
        sent = []
        runner = MagicMock(return_value="Tienes 2 correos sin leer.")
        s = ReminderService(store, notifier=sent.append, task_runner=runner)
        s.create("resume mis correos", "2030-03-10 08:00", is_task=True)
        s.run_due(_ts("2030-03-10 08:00"))
        assert "resume mis correos" in runner.call_args[0][0]
        assert sent == ["🗓️ *resume mis correos*\n\nTienes 2 correos sin leer."]

    def test_task_waits_for_runner(self, store):
        s = ReminderService(store, notifier=lambda t: True)
        s.create("x", "2030-03-10 08:00", is_task=True)
        with patch.object(rem, "_channel_task_runner", return_value=None):
            assert s.run_due(_ts("2030-03-10 08:00")) == 0

    def test_task_error_reported(self, store):
        sent = []
        s = ReminderService(
            store,
            notifier=sent.append,
            task_runner=MagicMock(side_effect=Exception("sin red")),
        )
        s.create("x", "2030-03-10 08:00", is_task=True)
        s.run_due(_ts("2030-03-10 08:00"))
        assert "No he podido hacerla: sin red" in sent[0]


class TestFacts:
    def test_add_list_delete(self, store):
        fid = store.add_fact("Su mujer se llama Ana")
        assert [f["text"] for f in store.facts()] == ["Su mujer se llama Ana"]
        assert store.delete_fact(fid) is True
        assert store.facts() == []

    def test_capped(self, store):
        for i in range(MAX_FACTS + 5):
            store.add_fact(f"dato {i}")
        facts = store.facts()
        assert len(facts) == MAX_FACTS
        assert facts[0]["text"] == "dato 5"


# ---------------------------------------------------------------------------
# Tools and wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def active(svc):
    with patch.object(rem, "_service", svc):
        yield svc


class TestTools:
    def test_registered(self):
        import importlib

        import openjarvis.tools.personal_tools as pt
        from openjarvis.core.registry import ToolRegistry

        importlib.reload(pt)
        for name in pt.PERSONAL_TOOLS:
            assert ToolRegistry.contains(name)

    def test_create_list_cancel(self, active):
        from openjarvis.tools.personal_tools import (
            ReminderCancelTool,
            ReminderCreateTool,
            ReminderListTool,
        )

        res = ReminderCreateTool().execute(
            text="reunión", when="2030-03-11 08:00", repeat="weekdays"
        )
        assert res.success
        assert "recordatorio «reunión»" in res.content
        assert "de lunes a viernes a las 08:00" in res.content
        assert "[1]" in ReminderListTool().execute().content
        assert ReminderCancelTool().execute(id=1).success
        assert ReminderCancelTool().execute(id=1).success is False
        assert "No hay" in ReminderListTool().execute().content

    def test_create_error(self, active):
        from openjarvis.tools.personal_tools import ReminderCreateTool

        res = ReminderCreateTool().execute(text="x", when="pronto")
        assert res.success is False

    def test_facts_reach_whatsapp_prompt(self, active):
        from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
        from openjarvis.tools.personal_tools import ForgetFactTool, RememberFactTool

        assert RememberFactTool().execute(fact="Prefiere  respuestas cortas").success
        prompt = WhatsAppBaileysChannel().system_prompt
        assert "Ahora es " in prompt
        assert "[1] Prefiere respuestas cortas" in prompt
        assert ForgetFactTool().execute(id=1).success
        assert "Prefiere" not in WhatsAppBaileysChannel().system_prompt


class TestWireChannel:
    def test_task_runner_and_fresh_prompt(self, tmp_path):
        from openjarvis.sessions.session import SessionStore
        from openjarvis.system.core import JarvisSystem

        system = JarvisSystem.__new__(JarvisSystem)
        system.session_store = SessionStore(db_path=tmp_path / "s.db")
        system.agent_name = "orchestrator"
        calls = []
        system.ask = lambda q, **kw: calls.append((q, kw)) or {"content": "hecho"}

        prompts = iter(["P1", "P2", "P3"])

        class Bridge:
            def __init__(self):
                self.task_runner = None
                self.handlers = []
                self.send = MagicMock()

            def on_message(self, handler):
                self.handlers.append(handler)

            @property
            def system_prompt(self):
                return next(prompts)

        bridge = Bridge()
        system.wire_channel(bridge)
        msg = SimpleNamespace(
            channel="c", conversation_id="1", sender="u", content="hola"
        )
        bridge.handlers[0](msg)
        bridge.handlers[0](msg)
        assert [kw["system_prompt"] for _, kw in calls] == ["P1", "P2"]
        assert bridge.task_runner("resume") == "hecho"
        assert calls[-1][1]["system_prompt"] == "P3"
        assert calls[-1][1]["agent"] == "orchestrator"
