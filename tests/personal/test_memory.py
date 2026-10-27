# ruff: noqa: E501
"""Permanent memory: log, facts (learned and edited), journal and recall."""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

import pytest

from openjarvis.personal.memory import LongMemory, MemoryKeeper


@pytest.fixture
def mem(tmp_path):
    m = LongMemory(tmp_path / "memory.db")
    yield m
    m.close()


class TestFacts:
    def test_add_dedupes_and_never_caps(self, mem):
        a = mem.add_fact("Óscar Peña es su socio")
        assert mem.add_fact("oscar pena es su socio") == a
        for i in range(120):
            mem.add_fact(f"Dato número {i}")
        assert len(mem.facts()) == 121

    def test_update_and_delete(self, mem):
        fid = mem.add_fact("Vive en Madrid")
        assert mem.update_fact(fid, "Vive en Getafe")
        assert mem.facts()[0]["text"] == "Vive en Getafe"
        assert mem.delete_fact(fid) and mem.facts() == []

    def test_apply_ops(self, mem):
        old = mem.add_fact("Trabaja en Acme")
        gone = mem.add_fact("Tiene un perro")
        n = mem.apply_ops(
            [
                {"op": "add", "kind": "persona", "text": "Su hija se llama Lucía"},
                {"op": "update", "id": old, "text": "Trabaja en AI Fast Track"},
                {"op": "delete", "id": gone},
                {"op": "nonsense"},
            ]
        )
        texts = {f["text"] for f in mem.facts()}
        assert n == 3
        assert texts == {"Su hija se llama Lucía", "Trabaja en AI Fast Track"}
        assert [f["source"] for f in mem.facts() if "Lucía" in f["text"]] == ["auto"]


class TestRecall:
    def test_search_log_without_accents(self, mem):
        mem.record(
            "user", "Mañana como con Óscar en el Tagliatella", ts=time.time() - 9e6
        )
        mem.record("user", "Compra leche")
        hits = mem.search_log("dónde comía con oscar")
        assert hits and "Tagliatella" in hits[0]["text"]

    def test_context_has_facts_journal_and_old_snippets(self, mem):
        mem.add_fact("Óscar Peña es su socio")
        mem.set_journal("2026-10-20", "- Habló de la ronda con Óscar")
        mem.record(
            "user", "La ronda con inversores cierra en enero", ts=time.time() - 5e6
        )
        ctx = mem.context("¿cómo va la ronda de inversores?")
        assert "Óscar Peña es su socio" in ctx
        assert "Habló de la ronda" in ctx
        assert "cierra en enero" in ctx
        assert "memoria permanente" in ctx

    def test_empty_memory_adds_nothing(self, mem):
        assert mem.context("hola") == ""


class TestKeeper:
    def test_learn_sends_known_facts_and_applies(self, mem):
        mem.add_fact("Trabaja en Acme")
        seen = []

        def generate(system, text, max_tokens):
            seen.append(text)
            return json.dumps([{"op": "add", "text": "Óscar Peña es su socio"}])

        keeper = MemoryKeeper(mem, generate)
        assert keeper.learn("Mi socio es Óscar Peña", "Apuntado") == 1
        assert "Trabaja en Acme" in seen[0]
        assert any("Óscar" in f["text"] for f in mem.facts())

    def test_learn_tolerates_bad_output(self, mem):
        keeper = MemoryKeeper(mem, lambda *a: "no sé")
        assert keeper.learn("hola qué tal", "bien") == 0

    def test_learn_skipped_without_credits(self, mem):
        keeper = MemoryKeeper(mem, lambda *a: "[]", allowed=lambda: False)
        assert keeper.learn("Mi socio es Óscar", "ok") == 0

    def test_journal_written_for_past_days_only(self, mem):
        today = date(2026, 10, 27)
        yesterday = datetime(2026, 10, 26, 10, 0).timestamp()
        mem.record("user", "Reunión con Óscar a las 9", ts=yesterday)
        mem.record("user", "Hoy compro el billete", ts=time.time())
        keeper = MemoryKeeper(mem, lambda s, t, n: "- Reunión con Óscar")
        # Only keep "yesterday" within the default 30-day look-back.
        days = mem.days_to_journal(today, max_days=4000)
        assert "2026-10-26" in days and today.isoformat() not in days
        keeper.write_journal(today)
        assert mem.journal()[0]["summary"] == "- Reunión con Óscar"
        assert "2026-10-26" not in mem.days_to_journal(today, max_days=4000)


def test_import_legacy_once(mem):
    old = time.time() - timedelta(days=40).total_seconds()
    msgs = [{"role": "user", "text": "Mi mujer se llama Ana", "ts": old}]
    mem.import_legacy(["Prefiere respuestas cortas"], msgs)
    mem.import_legacy(["Otra vez"], msgs)
    assert [f["text"] for f in mem.facts()] == ["Prefiere respuestas cortas"]
    assert mem.log_count() == 1


def test_memory_tools(tmp_path):
    from openjarvis.personal.memory import set_memory
    from openjarvis.tools.personal_tools import (
        ForgetFactTool,
        MemoryCorrectTool,
        MemoryRecallTool,
        RememberFactTool,
    )

    mem = LongMemory(tmp_path / "m.db")
    set_memory(mem)
    try:
        RememberFactTool().execute(fact="Óscar Peña es mi socio")
        fid = mem.facts()[0]["id"]
        MemoryCorrectTool().execute(id=fid, fact="Óscar Peña es su socio desde 2024")
        mem.record(
            "user", "Quedamos con Óscar en Getafe el lunes", ts=time.time() - 9e5
        )
        out = MemoryRecallTool().execute(query="oscar").content
        assert "desde 2024" in out and "Getafe" in out
        assert ForgetFactTool().execute(id=fid).success
        assert mem.facts() == []
    finally:
        set_memory(None)
        mem.close()
