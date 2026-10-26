"""Tests for bring-your-own AI account (server/ai_routes.py) and the launcher."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from openjarvis.server import ai_routes  # noqa: E402

KEY = "oj_sk_ai"
AUTH = {"Authorization": f"Bearer {KEY}"}
ANTHROPIC_KEY = "sk-ant-api03-" + "x" * 40


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_routes, "get_config_dir", lambda: tmp_path)
    for env in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "JARVIS_LAUNCHER",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("JARVIS_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("JARVIS_ENGINE", "cloud")
    return tmp_path


@pytest.fixture
def client(home):
    from starlette.testclient import TestClient

    from openjarvis.server.app import create_app

    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["m"]
    return TestClient(create_app(engine, "m", api_key=KEY))


class TestStatus:
    def test_none_then_included(self, client, monkeypatch):
        data = client.get("/api/ai/status", headers=AUTH).json()
        assert data["source"] == "none" and not data["configured"]
        assert {m["provider"] for m in data["models"]} == {
            "anthropic",
            "openai",
            "openrouter",
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "shared")
        assert client.get("/api/ai/status", headers=AUTH).json()["source"] == "included"

    def test_requires_auth(self, client):
        assert client.get("/api/ai/status").status_code == 401


class TestConfig:
    def test_saves_without_exposing_key(self, client, home):
        with (
            patch.object(ai_routes, "validate_key") as validate,
            patch.object(ai_routes, "restart_server", return_value=True) as restart,
        ):
            r = client.post(
                "/api/ai/config",
                json={
                    "provider": "anthropic",
                    "api_key": ANTHROPIC_KEY,
                    "model": "claude-haiku-5-5",
                },
                headers=AUTH,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "own" and body["restarting"] is True
        assert ANTHROPIC_KEY not in r.text and body["key_hint"].endswith("xxxx")
        validate.assert_called_once()
        restart.assert_called_once()
        saved = json.loads((home / "ai.json").read_text())
        assert saved["env"] == {
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "JARVIS_MODEL": "claude-haiku-5-5",
            "JARVIS_ENGINE": "cloud",
        }

    @pytest.mark.parametrize(
        "body,msg",
        [
            ({"provider": "acme", "api_key": ANTHROPIC_KEY, "model": "x"}, "Proveedor"),
            (
                {
                    "provider": "anthropic",
                    "api_key": "short",
                    "model": "claude-haiku-5-5",
                },
                "clave",
            ),
            (
                {
                    "provider": "anthropic",
                    "api_key": ANTHROPIC_KEY,
                    "model": "gpt-5-mini",
                },
                "modelo",
            ),
        ],
    )
    def test_rejects_bad_input(self, client, body, msg):
        r = client.post("/api/ai/config", json=body, headers=AUTH)
        assert r.status_code == 400 and msg in r.json()["detail"]

    def test_rejected_key(self, client, home):
        with patch.object(
            ai_routes,
            "validate_key",
            side_effect=ValueError("El proveedor no acepta esa clave."),
        ):
            r = client.post(
                "/api/ai/config",
                json={
                    "provider": "anthropic",
                    "api_key": ANTHROPIC_KEY,
                    "model": "claude-haiku-5-5",
                },
                headers=AUTH,
            )
        assert r.status_code == 400 and not (home / "ai.json").exists()

    def test_delete(self, client, home):
        (home / "ai.json").write_text('{"provider": "openai"}')
        with patch.object(ai_routes, "restart_server", return_value=False):
            r = client.request("DELETE", "/api/ai/config", headers=AUTH)
        assert r.status_code == 200 and not (home / "ai.json").exists()


class TestValidateKey:
    def _http(self, status):
        return httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(status, json={}))
        )

    def test_ok_and_rejected(self):
        ai_routes.validate_key("anthropic", ANTHROPIC_KEY, http=self._http(200))
        with pytest.raises(ValueError, match="no acepta"):
            ai_routes.validate_key("openai", "sk-" + "y" * 30, http=self._http(401))

    def test_openrouter_endpoint(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={})

        ai_routes.validate_key(
            "openrouter",
            "sk-or-" + "z" * 30,
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert seen == ["https://openrouter.ai/api/v1/key"]


def test_restart_noop_without_launcher(monkeypatch):
    monkeypatch.delenv("JARVIS_LAUNCHER", raising=False)
    assert ai_routes.restart_server() is False


def _load_bootstrap():
    path = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "railway"
        / "bootstrap_config.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_launcher_applies_user_ai(tmp_path, monkeypatch):
    pytest.importorskip("tomlkit")
    boot = _load_bootstrap()
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_MODEL", "claude-sonnet-5-5")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "ai.json").write_text(
        json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "sk-user",
                    "JARVIS_MODEL": "gpt-5-mini",
                    "EVIL": "x",
                }
            }
        )
    )
    boot.apply_ai_overrides()
    import os

    assert os.environ["OPENAI_API_KEY"] == "sk-user"
    assert os.environ["JARVIS_MODEL"] == "gpt-5-mini"
    assert "EVIL" not in os.environ
