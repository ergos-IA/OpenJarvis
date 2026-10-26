"""Tests for the AI Fast Track model catalog, Railway client and console."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from starlette.testclient import TestClient  # noqa: E402

from openjarvis.fasttrack import admin as admin_mod  # noqa: E402
from openjarvis.fasttrack.admin import Auth, create_admin_app, slugify  # noqa: E402
from openjarvis.fasttrack.models import describe, provider_for  # noqa: E402
from openjarvis.fasttrack.railway import RailwayClient, RailwayError  # noqa: E402

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestModels:
    def test_known(self):
        d = describe("claude-sonnet-5-5")
        assert d["label"] == "Claude Sonnet 5.5"
        assert d["provider"] == "anthropic" and d["open_source"] is False

    def test_inferred(self):
        assert provider_for("openrouter/foo/bar") == "openrouter"
        assert provider_for("claude-future") == "anthropic"
        assert provider_for("gpt-9") == "openai"
        assert provider_for("llama3", engine="ollama") == "ollama"
        d = describe("openrouter/foo/bar-7b")
        assert d["label"] == "bar-7b" and d["open_source"] is True


def test_slugify():
    assert slugify("José María López") == "jose-maria-lopez"
    assert slugify("!!!") == "usuario"


class TestAuth:
    def test_session_roundtrip(self):
        auth = Auth("pw", "svc")
        token = auth.issue()
        assert auth.valid(token)
        assert not auth.valid(token + "x")
        assert not Auth("other", "svc").valid(token)
        assert not auth.valid("123.abc")

    def test_throttle(self):
        auth = Auth("pw", "svc")
        for _ in range(5):
            assert auth.check_password("bad", "1.2.3.4") is False
        with pytest.raises(Exception, match="Demasiados"):
            auth.check_password("pw", "1.2.3.4")
        assert auth.check_password("pw", "5.6.7.8") is True


# ---------------------------------------------------------------------------
# Railway client
# ---------------------------------------------------------------------------


def _client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return RailwayClient("tok", "P", "E", http=http)


class TestRailwayClient:
    def test_unconfigured(self):
        with pytest.raises(RailwayError, match="RAILWAY_API_TOKEN"):
            RailwayClient("", "P", "E").gql("{ x }")

    def test_errors_raised(self):
        c = _client(
            lambda r: httpx.Response(
                200, json={"errors": [{"message": "Not Authorized"}]}
            )
        )
        with pytest.raises(RailwayError, match="Not Authorized"):
            c.domain("S")

    def test_overview_maps_volumes(self):
        def handler(request):
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "project": {
                            "services": {
                                "edges": [
                                    {
                                        "node": {
                                            "id": "S1",
                                            "name": "jarvis-ana",
                                            "createdAt": "2026",
                                            "deployments": {
                                                "edges": [
                                                    {"node": {"status": "SUCCESS"}}
                                                ]
                                            },
                                        }
                                    }
                                ]
                            },
                            "volumes": {
                                "edges": [
                                    {
                                        "node": {
                                            "id": "V1",
                                            "name": "v",
                                            "volumeInstances": {
                                                "edges": [
                                                    {
                                                        "node": {
                                                            "serviceId": "S1",
                                                            "mountPath": "/data",
                                                        }
                                                    }
                                                ]
                                            },
                                        }
                                    }
                                ]
                            },
                        }
                    }
                },
            )

        [svc] = _client(handler).project_overview()["services"]
        assert svc == {
            "id": "S1",
            "name": "jarvis-ana",
            "created_at": "2026",
            "deploy_status": "SUCCESS",
            "volume_id": "V1",
        }

    def test_set_variables_skips_deploy(self):
        sent = {}

        def handler(request):
            sent.update(json.loads(request.content))
            return httpx.Response(
                200, json={"data": {"variableCollectionUpsert": True}}
            )

        _client(handler).set_variables("S", {"A": "1"})
        assert sent["variables"]["i"]["skipDeploys"] is True
        assert sent["variables"]["i"]["serviceId"] == "S"


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------


class FakeRailway:
    configured = True

    def __init__(self):
        self.services = [
            {
                "id": "ADMIN",
                "name": "consola",
                "created_at": "1",
                "deploy_status": "SUCCESS",
                "volume_id": "",
            },
            {
                "id": "S1",
                "name": "jarvis-ana",
                "created_at": "2",
                "deploy_status": "SUCCESS",
                "volume_id": "V1",
            },
        ]
        self.vars = {
            "S1": {
                "OPENJARVIS_API_KEY": "oj_sk_ana",
                "JARVIS_MODEL": "claude-haiku-5-5",
                "JARVIS_USER_NAME": "Ana Pérez",
                "JARVIS_USER_EMAIL": "ana@x.com",
            },
            "": {"ANTHROPIC_API_KEY": "x"},
        }
        self.calls = []

    def project_overview(self):
        return {"services": list(self.services)}

    def variables(self, service_id=""):
        return self.vars.get(service_id, {})

    def domain(self, service_id):
        return f"{service_id.lower()}.up.railway.app"

    def create_service(self, name):
        self.calls.append(("create", name))
        self.services.append(
            {
                "id": "NEW",
                "name": name,
                "created_at": "3",
                "deploy_status": "",
                "volume_id": "",
            }
        )
        return "NEW"

    def set_variables(self, sid, variables, deploy=False):
        self.calls.append(("vars", sid, variables, deploy))

    def create_volume(self, sid, mount):
        self.calls.append(("volume", sid, mount))

    def create_domain(self, sid, port):
        return "new.up.railway.app"

    def set_healthcheck(self, sid, path):
        self.calls.append(("health", sid, path))

    def connect_repo(self, sid, repo, branch):
        self.calls.append(("connect", sid, repo, branch))

    def redeploy(self, sid):
        self.calls.append(("redeploy", sid))

    def delete_service(self, sid, volume_id):
        self.calls.append(("delete", sid, volume_id))


@pytest.fixture
def rw():
    return FakeRailway()


@pytest.fixture
def console(rw, monkeypatch):
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "ADMIN")

    def live(request):
        if request.url.path == "/api/whatsapp/status":
            return httpx.Response(200, json={"connected": True, "me": "34600"})
        return httpx.Response(
            200,
            json={"gmail": {"state": "connected"}, "outlook": {"state": "unavailable"}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(live))
    app = create_admin_app(rw, password="secreto", http=http)
    return TestClient(app)


H = {"X-Requested-With": "fasttrack"}


def _login(client):
    r = client.post("/api/login", json={"password": "secreto"})
    assert r.status_code == 200


class TestConsole:
    def test_page_and_health_public(self, console):
        assert console.get("/health").json() == {"status": "ok"}
        r = console.get("/")
        assert r.status_code == 200 and "Consola Jarvis" in r.text
        assert console.get("/jarvis-ui/aifasttrack-logo.svg").status_code == 200

    def test_api_requires_login(self, console):
        assert console.get("/api/overview").status_code == 401
        assert console.post("/api/login", json={"password": "nope"}).status_code == 401

    def test_csrf_header_required(self, console):
        _login(console)
        r = console.post("/api/users", json={"name": "x"})
        assert r.status_code == 403

    def test_overview(self, console):
        _login(console)
        data = console.get("/api/overview").json()
        assert data["credentials"]["ANTHROPIC_API_KEY"] is True
        assert data["credentials"]["OPENAI_API_KEY"] is False
        [user] = data["users"]  # the console's own service is not listed
        assert user["name"] == "Ana Pérez"
        assert user["model"]["label"] == "Claude Haiku 5.5"
        assert user["link"] == "https://s1.up.railway.app/#key=oj_sk_ana"
        assert user["whatsapp"] is True and user["mail"] == ["gmail"]

    def test_create_user(self, console, rw):
        _login(console)
        r = console.post(
            "/api/users",
            json={
                "name": "Ana Pérez",
                "email": "a@x.com",
                "model": "openrouter/qwen/qwen3-235b-a22b",
            },
            headers=H,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["service"] == "jarvis-ana-perez"
        assert body["link"].startswith("https://new.up.railway.app/#key=oj_sk_")
        _, sid, variables, deploy = next(c for c in rw.calls if c[0] == "vars")
        assert deploy is False
        assert variables["JARVIS_ENGINE"] == "cloud"
        assert variables["JARVIS_USER_NAME"] == "Ana Pérez"
        # By default the customer connects their own AI: no shared keys.
        assert "OPENROUTER_API_KEY" not in variables
        assert variables["LEGAL_COMPANY"] == "${{shared.LEGAL_COMPANY}}"
        assert variables["RAILWAY_DOCKERFILE_PATH"] == "deploy/railway/Dockerfile"
        assert ("volume", "NEW", "/data") in rw.calls
        assert ("connect", "NEW", "ergos-IA/OpenJarvis", "main") in rw.calls

    def test_create_user_with_included_ai(self, console, rw):
        _login(console)
        r = console.post(
            "/api/users", json={"name": "Ana", "ai": "included"}, headers=H
        )
        assert r.status_code == 200
        _, _, variables, _ = next(c for c in rw.calls if c[0] == "vars")
        assert variables["ANTHROPIC_API_KEY"] == "${{shared.ANTHROPIC_API_KEY}}"

    def test_create_pro_plan(self, console, rw):
        _login(console)
        r = console.post(
            "/api/users",
            json={"name": "Eva", "plan": "pro", "model": "claude-haiku-5-5"},
            headers=H,
        )
        assert r.status_code == 200
        _, _, variables, _ = next(c for c in rw.calls if c[0] == "vars")
        assert variables["JARVIS_PLAN"] == "pro"
        assert (
            variables["ANTHROPIC_API_KEY"] == "${{shared.ANTHROPIC_API_KEY}}"
        )  # AI included
        assert variables["FASTTRACK_ADMIN_TOKEN"] == "${{shared.FASTTRACK_ADMIN_TOKEN}}"

    def test_duplicate_names_get_suffix(self, console, rw):
        _login(console)
        r = console.post("/api/users", json={"name": "Ana"}, headers=H)
        assert r.json()["service"] == "jarvis-ana-2"

    def test_create_reports_github_problem(self, console, rw):
        rw.connect_repo = MagicMock(side_effect=RailwayError("repo not found"))
        _login(console)
        r = console.post("/api/users", json={"name": "Luis"}, headers=H)
        assert r.status_code == 200 and "repo not found" in r.json()["build_error"]

    def test_change_model_to_ollama(self, console, rw):
        _login(console)
        r = console.patch(
            "/api/users/S1",
            json={"model": "llama3.1:8b", "provider": "ollama"},
            headers=H,
        )
        assert r.status_code == 200
        assert (
            "vars",
            "S1",
            {"JARVIS_MODEL": "llama3.1:8b", "JARVIS_ENGINE": "ollama"},
            True,
        ) in rw.calls

    def test_cannot_touch_console_service(self, console):
        _login(console)
        assert (
            console.patch(
                "/api/users/ADMIN", json={"model": "x"}, headers=H
            ).status_code
            == 404
        )

    def test_delete_needs_confirmation(self, console, rw):
        _login(console)
        r = console.request(
            "DELETE", "/api/users/S1", json={"confirm": "otro"}, headers=H
        )
        assert r.status_code == 400
        r = console.request(
            "DELETE", "/api/users/S1", json={"confirm": "Ana"}, headers=H
        )
        assert r.status_code == 200
        assert ("delete", "S1", "V1") in rw.calls

    def test_rotate_key(self, console, rw):
        _login(console)
        r = console.post("/api/users/S1/rotate-key", headers=H)
        link = r.json()["link"]
        assert link.startswith("https://s1.up.railway.app/#key=oj_sk_")
        _, _, variables, deploy = rw.calls[-1]
        assert deploy is True and link.endswith(variables["OPENJARVIS_API_KEY"])

    def test_redeploy_connects_unbuilt_service(self, console, rw):
        rw.services[1]["deploy_status"] = ""
        _login(console)
        console.post("/api/users/S1/redeploy", headers=H)
        assert ("connect", "S1", "ergos-IA/OpenJarvis", "main") in rw.calls

    def test_no_password_configured(self, rw):
        client = TestClient(create_admin_app(rw, password=""))
        assert client.post("/api/login", json={"password": ""}).status_code == 503


def test_new_api_key_unique():
    assert admin_mod.new_api_key() != admin_mod.new_api_key()
