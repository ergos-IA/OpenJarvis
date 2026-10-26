"""Tests for the integrations framework, its routes and the OAuth relay."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import (
    Field,
    Integration,
    IntegrationError,
    OAuthSpec,
    S,
    register,
    tool,
)
from openjarvis.integrations.manager import (
    IntegrationManager,
    decode_state,
    encode_state,
)


@pytest.fixture
def api():
    """Route base.http() to a handler the test sets: api.handler = fn(request)."""
    holder = SimpleNamespace(handler=lambda r: httpx.Response(404), calls=[])

    def dispatch(request):
        holder.calls.append(request)
        return holder.handler(request)

    base.set_http(httpx.Client(transport=httpx.MockTransport(dispatch)))
    yield holder
    base.set_http(None)


def _fake(checks=None):
    @tool("fake_echo", "Echo", {"x": S("text")}, ["x"], integration="fake")
    def echo(x):
        return f"echo {x}"

    @tool("fake_write", "Write", {}, writes=True, integration="fake")
    def write():
        return None

    def check(creds):
        if creds["token"] == "bad":
            raise IntegrationError("Clave no válida")
        return "cuenta fake"

    return register(
        Integration(
            id="fake",
            name="Fake",
            category="tecnico",
            description="d",
            abilities=[],
            steps=[],
            fields=[Field("token", "Token"), Field("extra", "Extra", optional=True)],
            check=check,
            build_tools=lambda creds: [echo, write],
            prompt="Usa fake_echo.",
        )
    )


@pytest.fixture
def mgr(tmp_path):
    _fake()
    yield IntegrationManager(tmp_path / "integrations.json")
    base._REGISTRY.pop("fake", None)
    base._REGISTRY.pop("fakeoauth", None)


class TestManager:
    def test_catalog_lists_builtins(self, mgr):
        ids = {i["id"] for i in mgr.catalog()}
        assert {"agenda", "notion", "weather", "mcp", "fake"} <= ids
        weather = next(i for i in mgr.catalog() if i["id"] == "weather")
        assert weather["connected"] and weather["always_on"]

    def test_connect_validates_and_persists(self, mgr, tmp_path):
        with pytest.raises(IntegrationError, match="Falta"):
            mgr.connect("fake", {})
        with pytest.raises(IntegrationError, match="no válida"):
            mgr.connect("fake", {"token": "bad"})
        st = mgr.connect("fake", {"token": " good ", "extra": ""})
        assert st["connected"] and st["account"] == "cuenta fake"
        saved = json.loads((tmp_path / "integrations.json").read_text())
        assert saved["fake"]["creds"] == {"token": "good"}
        again = IntegrationManager(tmp_path / "integrations.json")
        assert again.connected("fake")

    def test_tools_follow_connection_and_attach(self, mgr):
        system = SimpleNamespace(
            tools=[SimpleNamespace(spec=SimpleNamespace(name="think"))]
        )
        mgr.attach(system)
        names = {t.spec.name for t in system.tools}
        assert "weather_forecast" in names and "fake_echo" not in names
        mgr.connect("fake", {"token": "good"})
        names = {t.spec.name for t in system.tools}
        assert {"think", "fake_echo", "fake_write"} <= names
        assert "Fake (cuenta fake)" in mgr.prompt_context()
        mgr.disconnect("fake")
        assert "fake_echo" not in {t.spec.name for t in system.tools}
        mgr.attach(system)  # re-attaching does not duplicate our tools
        assert [t.spec.name for t in system.tools].count("weather_forecast") == 1

    def test_tool_results_and_errors(self, mgr):
        mgr.connect("fake", {"token": "good"})
        tools = {t.spec.name: t for t in mgr.tools()}
        assert tools["fake_echo"].execute(x="hola").content == "echo hola"
        assert "confirme" in tools["fake_write"].spec.description
        assert tools["fake_write"].execute().content == "Hecho."
        bad = tools["fake_echo"].execute()
        assert not bad.success and "Parámetros" in bad.content

    def test_unknown(self, mgr):
        with pytest.raises(IntegrationError):
            mgr.connect("nope", {})


class TestOAuth:
    @pytest.fixture
    def oauth(self, mgr, monkeypatch):
        monkeypatch.setenv("OPENJARVIS_FAKEO_CLIENT_ID", "cid")
        monkeypatch.setenv("OPENJARVIS_FAKEO_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("PUBLIC_URL", "https://site.test")

        @tool("fakeo_me", "Me", integration="fakeoauth")
        def me():
            return "ok"

        register(
            Integration(
                id="fakeoauth",
                name="FakeO",
                category="productividad",
                description="d",
                abilities=[],
                steps=[],
                oauth=OAuthSpec(
                    provider="fakeo",
                    authorize_url="https://auth.test/authorize",
                    token_url="https://auth.test/token",
                    scopes=["a", "b"],
                    client_env="FAKEO",
                    extra_params={"access_type": "offline"},
                ),
                check=lambda creds: f"user via {creds.token()}",
                build_tools=lambda creds: [me],
            )
        )
        return mgr

    def test_full_flow_and_refresh(self, oauth, api):
        url = oauth.begin_oauth("fakeoauth", "jarvis-ana.up.railway.app")
        q = parse_qs(urlparse(url).query)
        assert q["redirect_uri"] == ["https://site.test/oauth/callback"]
        assert q["scope"] == ["a b"] and q["access_type"] == ["offline"]
        state = q["state"][0]
        assert decode_state(state)["h"] == "jarvis-ana.up.railway.app"

        def token_endpoint(request):
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["the-code"] and form["client_secret"] == [
                    "csecret"
                ]
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at1",
                        "refresh_token": "rt",
                        "expires_in": 3600,
                    },
                )
            assert form["refresh_token"] == ["rt"]
            return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})

        api.handler = token_endpoint
        assert oauth.finish_oauth("the-code", state) == "fakeoauth"
        st = oauth.status("fakeoauth")
        assert st["connected"] and st["account"] == "user via at1"
        # Nonces are single use.
        with pytest.raises(IntegrationError, match="caducado"):
            oauth.finish_oauth("the-code", state)
        # Expired access token -> refreshed transparently, refresh token kept.
        oauth._data["fakeoauth"]["creds"]["expires_at"] = time.time() - 10
        assert oauth._fresh_token("fakeoauth") == "at2"
        assert oauth._data["fakeoauth"]["creds"]["refresh_token"] == "rt"

    def test_not_configured(self, mgr):
        register(
            Integration(
                id="fakeoauth",
                name="X",
                category="tecnico",
                description="",
                abilities=[],
                steps=[],
                oauth=OAuthSpec(
                    "x", "https://a", "https://t", [], client_env="MISSING"
                ),
            )
        )
        assert not mgr.status("fakeoauth")["available"]
        with pytest.raises(IntegrationError, match="activado"):
            mgr.begin_oauth("fakeoauth", "h")

    def test_state_roundtrip_and_garbage(self):
        assert decode_state(encode_state("h.example", "n1")) == {
            "h": "h.example",
            "n": "n1",
        }
        with pytest.raises(IntegrationError):
            decode_state("%%%")


def test_fetch_public_blocks_private(api):
    with pytest.raises(IntegrationError, match="no permitido"):
        base.fetch_public("http://127.0.0.1/x.ics")
    with pytest.raises(IntegrationError, match="https"):
        base.fetch_public("file:///etc/passwd")


# ---------------------------------------------------------------------------
# Routes (instance) and relay (public site)
# ---------------------------------------------------------------------------

KEY = "oj_sk_int"


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from openjarvis.integrations import manager as manager_mod
    from openjarvis.server.app import create_app

    _fake()
    monkeypatch.setattr(
        manager_mod, "_manager", IntegrationManager(tmp_path / "i.json")
    )
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["m"]
    yield TestClient(create_app(engine, "m", api_key=KEY))
    base._REGISTRY.pop("fake", None)


AUTH = {"Authorization": f"Bearer {KEY}"}


class TestRoutes:
    def test_requires_auth(self, client):
        assert client.get("/api/integrations").status_code == 401

    def test_connect_list_disconnect(self, client):
        r = client.post("/api/integrations/fake", json={"token": "bad"}, headers=AUTH)
        assert r.status_code == 400 and "no válida" in r.json()["detail"]
        r = client.post("/api/integrations/fake", json={"token": "good"}, headers=AUTH)
        assert r.status_code == 200 and r.json()["account"] == "cuenta fake"
        items = client.get("/api/integrations", headers=AUTH).json()["integrations"]
        assert next(i for i in items if i["id"] == "fake")["connected"]
        assert "token" not in json.dumps(
            next(i for i in items if i["id"] == "fake")["account"]
        )
        assert client.delete("/api/integrations/fake", headers=AUTH).status_code == 200
        assert client.delete("/api/integrations/nope", headers=AUTH).status_code == 404

    def test_oauth_done_rejects_unknown_state(self, client):
        r = client.get(
            "/oauth/done", params={"code": "c", "state": encode_state("h", "zz")}
        )
        assert r.status_code == 400 and "caducado" in r.text
        r = client.get("/oauth/done", params={"error": "access_denied"})
        assert r.status_code == 400


class TestRelay:
    @pytest.fixture
    def site(self):
        from starlette.testclient import TestClient

        from openjarvis.fasttrack.landing import create_landing_app

        class RW:
            def project_overview(self):
                return {"services": [{"id": "S1", "name": "jarvis-ana"}]}

            def variables(self, sid):
                return {
                    "OPENJARVIS_API_KEY": "oj_sk_" + "a" * 43,
                    "JARVIS_ACCESS_CODE": "ABCD-EFGH-JKMN",
                }

            def domain(self, sid):
                return "jarvis-ana.up.railway.app"

        return TestClient(create_landing_app(RW()), follow_redirects=False)

    def test_bounces_to_known_instance(self, site):
        state = encode_state("jarvis-ana.up.railway.app", "n")
        r = site.get("/oauth/callback", params={"code": "c1", "state": state})
        assert r.status_code == 303
        loc = urlparse(r.headers["location"])
        assert loc.netloc == "jarvis-ana.up.railway.app" and loc.path == "/oauth/done"
        assert parse_qs(loc.query) == {"code": ["c1"], "state": [state]}

    def test_refuses_foreign_host(self, site):
        r = site.get(
            "/oauth/callback",
            params={"code": "c", "state": encode_state("evil.example", "n")},
        )
        assert r.status_code == 400
        assert (
            site.get("/oauth/callback", params={"state": "garbage"}).status_code == 400
        )
