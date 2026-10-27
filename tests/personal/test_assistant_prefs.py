"""Assistant behaviour settings and their HTTP route."""

from __future__ import annotations

import json

import pytest

from openjarvis.personal.assistant_prefs import AssistantPrefs


def test_defaults():
    st = AssistantPrefs().settings()
    assert st["confirm"] == "smart" and st["style"] == "brief"
    assert st["engine"] == {
        "temperature": 0.5,
        "max_tokens": 2048,
        "max_turns": 15,
    }


def test_persists_and_clamps(tmp_path):
    path = tmp_path / "assistant.json"
    p = AssistantPrefs(path)
    assert p.update(engine={"max_turns": 999, "temperature": "0.2"}) is True
    assert p.update(confirm="never") is False
    again = AssistantPrefs(path)
    assert again.engine()["max_turns"] == 40
    assert again.engine()["temperature"] == 0.2
    assert again.live()["confirm"] == "never"
    assert json.loads(path.read_text("utf-8"))["engine"]["max_turns"] == 40


def test_same_engine_values_do_not_restart(tmp_path):
    p = AssistantPrefs(tmp_path / "a.json")
    p.update(engine={"max_turns": 20})
    assert p.update(engine={"max_turns": 20}) is False


def test_rejects_bad_values():
    p = AssistantPrefs()
    with pytest.raises(ValueError):
        p.update(confirm="sometimes")
    with pytest.raises(ValueError):
        p.update(engine={"temperature": "hot"})


def test_empty_text_clears():
    p = AssistantPrefs()
    p.update(instructions="Firma como Pepe", assistant_name="Viernes")
    p.update(instructions="", assistant_name="")
    assert p.live()["instructions"] == "" and p.live()["assistant_name"] == ""


def test_prompt_rules():
    p = AssistantPrefs()
    assert (
        "no pidas confirmación para lo que el usuario te ha pedido" in p.prompt_rules()
    )
    p.update(confirm="always", style="detailed", instructions="Háblame en inglés")
    rules = p.prompt_rules()
    assert "espera su «sí»" in rules and "detalladas" in rules
    assert "Háblame en inglés" in rules


def test_bootstrap_copies_engine_settings(tmp_path):
    import importlib.util
    from pathlib import Path

    import tomlkit

    spec = importlib.util.spec_from_file_location(
        "bootstrap_config",
        Path(__file__).parents[2] / "deploy" / "railway" / "bootstrap_config.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    AssistantPrefs(tmp_path / "assistant.json").update(
        engine={"max_turns": 25}
    )
    doc = tomlkit.document()
    mod._apply_assistant_settings(doc, tmp_path)
    assert doc["agent"]["max_turns"] == 25
    assert doc["sessions"]["max_age_hours"] > 24 * 365 * 10
    assert doc["intelligence"]["max_tokens"] == 2048


def test_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.personal import assistant_prefs
    from openjarvis.server import ai_routes
    from openjarvis.server.assistant_routes import router

    assistant_prefs.set_prefs(AssistantPrefs())
    restarts = []
    monkeypatch.setattr(ai_routes, "restart_server", lambda: restarts.append(1) or True)
    try:
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app)
        assert c.get("/api/assistant/settings").json()["confirm"] == "smart"
        r = c.post("/api/assistant/settings", json={"confirm": "never"}).json()
        assert r["confirm"] == "never" and r["restarting"] is False
        r = c.post("/api/assistant/settings", json={"engine": {"max_turns": 30}}).json()
        assert r["restarting"] is True and restarts == [1]
        assert c.post("/api/assistant/settings", json={"style": "x"}).status_code == 400
    finally:
        assistant_prefs.set_prefs(None)
