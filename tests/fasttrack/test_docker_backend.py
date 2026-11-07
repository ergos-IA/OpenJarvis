"""Docker backend (VPS): Docker Engine API mocked with httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from starlette.testclient import TestClient  # noqa: E402

from openjarvis.fasttrack import billing, dockerctl  # noqa: E402
from openjarvis.fasttrack.admin import create_admin_app  # noqa: E402
from openjarvis.fasttrack.backends import (  # noqa: E402
    RailwayBackend,
    get_backend,
)
from openjarvis.fasttrack.docker_backend import (  # noqa: E402
    LABEL_RAILWAY,
    DockerBackend,
    DockerError,
    parse_env,
    substitute_shared,
    traefik_labels,
)
from openjarvis.fasttrack.emails import instance_link  # noqa: E402
from openjarvis.fasttrack.landing import create_landing_app  # noqa: E402
from openjarvis.fasttrack.provisioning import provision_user  # noqa: E402

KEY = "oj_sk_" + "d" * 43
IMAGE_ENV = [
    "PATH=/usr/local/bin:/usr/bin",
    "HOME=/home/openjarvis",
    "TZ=Europe/Madrid",
]


class FakeDocker:
    """Just enough of the Docker Engine API, with state."""

    def __init__(self):
        self.containers = {}
        self.volumes = {}
        self.images = {"openjarvis:latest": list(IMAGE_ENV)}
        self.requests = []
        self.fail_create = False

    def _json(self, status, data=None):
        if data is None:
            return httpx.Response(status)
        return httpx.Response(status, json=data)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        assert path.startswith("/v1.44/"), path
        path = path[len("/v1.44") :]
        query = parse_qs(urlparse(str(request.url)).query)
        m = request.method
        if m == "GET" and path == "/containers/json":
            wanted = json.loads(query["filters"][0])["label"][0]
            k, _, v = wanted.partition("=")
            out = []
            for name, c in self.containers.items():
                if c["Config"]["Labels"].get(k) == v:
                    out.append(
                        {
                            "Names": ["/" + name],
                            "Labels": c["Config"]["Labels"],
                            "State": c["State"]["Status"],
                            "Status": c["status_text"],
                            "Created": 1_700_000_000,
                        }
                    )
            return self._json(200, out)
        mc = re.fullmatch(r"/containers/([^/]+)(/json|/start|/stop)?", path)
        if path == "/containers/create" and m == "POST":
            name = query["name"][0]
            if name in self.containers:
                return self._json(409, {"message": f"Conflict: {name}"})
            body = json.loads(request.content)
            if self.fail_create:
                self.fail_create = False
                return self._json(500, {"message": "boom"})
            if body["Image"] not in self.images:
                return self._json(404, {"message": f"No such image: {body['Image']}"})
            env = dict(e.split("=", 1) for e in self.images[body["Image"]])
            env.update(dict(e.split("=", 1) for e in body["Env"]))
            self.containers[name] = {
                "Image": "sha256:" + body["Image"],
                "Config": {
                    "Image": body["Image"],
                    "Env": [f"{k}={v}" for k, v in env.items()],
                    "Labels": body["Labels"],
                },
                "HostConfig": body["HostConfig"],
                "body": body,
                "State": {"Status": "created"},
                "status_text": "Created",
            }
            return self._json(201, {"Id": "x" + name})
        if mc:
            name, action = mc.group(1), mc.group(2)
            c = self.containers.get(name)
            if c is None:
                return self._json(404, {"message": f"No such container: {name}"})
            if m == "GET" and action == "/json":
                return self._json(200, c)
            if m == "POST" and action == "/start":
                c["State"]["Status"] = "running"
                c["status_text"] = "Up 1 second (health: starting)"
                return self._json(204)
            if m == "POST" and action == "/stop":
                c["State"]["Status"] = "exited"
                return self._json(204)
            if m == "DELETE" and action is None:
                del self.containers[name]
                return self._json(204)
        mv = re.fullmatch(r"/volumes/([^/]+)", path)
        if path == "/volumes/create" and m == "POST":
            body = json.loads(request.content)
            self.volumes.setdefault(body["Name"], body)
            return self._json(201, body)
        if mv:
            name = mv.group(1)
            if name not in self.volumes:
                return self._json(404, {"message": f"get {name}: no such volume"})
            if m == "GET":
                return self._json(200, self.volumes[name])
            if m == "DELETE":
                del self.volumes[name]
                return self._json(204)
        mi = re.fullmatch(r"/images/(.+)/json", path)
        if mi and m == "GET":
            ref = mi.group(1).removeprefix("sha256:")
            if ref not in self.images:
                return self._json(404, {"message": "No such image"})
            return self._json(200, {"Config": {"Env": self.images[ref]}})
        return self._json(500, {"message": f"unexpected {m} {path}"})


@pytest.fixture
def shared_env(tmp_path):
    path = tmp_path / "shared.env"
    path.write_text(
        "# shared\n"
        "FASTTRACK_ADMIN_TOKEN=adm-token\n"
        "export LEGAL_COMPANY='Fast Factory IA Engine S.L.'\n"
        'ANTHROPIC_API_KEY="sk-ant-1"\n'
        "MAILJET_API_KEY=mj\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def docker(shared_env, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://jarvis.aifasttrack.tech")
    fake = FakeDocker()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    backend = DockerBackend(
        "tcp://docker-proxy:2375",
        http=http,
        base_url="http://docker-proxy:2375",
        shared_env=str(shared_env),
        image="openjarvis:latest",
        network="root_default",
    )
    return fake, backend


def _env(container):
    return dict(e.split("=", 1) for e in container["Config"]["Env"])


def _adopt(backend, slug="ana", railway_id="", reuse=True, **extra):
    env = {
        "OPENJARVIS_API_KEY": KEY,
        "JARVIS_ACCESS_CODE": "ABCD-EFGH-JKMN",
        "JARVIS_USER_NAME": "Ana",
        "JARVIS_PLAN": "esencial",
        "STRIPE_CUSTOMER_ID": "cus_1",
        **extra,
    }
    return backend.create_container(
        slug, env, railway_id=railway_id, reuse_volume=reuse
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_parse_env():
    env = parse_env("# c\n\nA=1\nexport B='x y'\nC=\"q\"\nbad line\nD=a=b\n")
    assert env == {"A": "1", "B": "x y", "C": "q", "D": "a=b"}


def test_substitute_shared():
    env, refs = substitute_shared(
        {
            "A": "${{shared.A}}",
            "B": "${{shared.OTHER}}",
            "C": "plain",
            "D": "pre-${{shared.A}}",
            "E": "${{shared.MISSING}}",
        },
        {"A": "1", "OTHER": "2"},
    )
    assert env == {"A": "1", "B": "2", "C": "plain", "D": "pre-1", "E": ""}
    assert refs == ["A", "B:OTHER", "E:MISSING"]


def test_traefik_labels():
    labels = traefik_labels(
        "ana-perez", host="jarvis.aifasttrack.tech", network="root_default"
    )
    assert labels["traefik.http.routers.jv-ana-perez.rule"] == (
        "Host(`jarvis.aifasttrack.tech`) && "
        "HeaderRegexp(`Cookie`, `(^|;\\s*)jv=ana-perez(;|$)`)"
    )
    assert labels["traefik.http.routers.jv-ana-perez.priority"] == "100"
    assert labels["traefik.http.routers.jv-ana-perez.entrypoints"] == "websecure"
    assert (
        labels["traefik.http.routers.jv-ana-perez.tls.certresolver"] == "mytlschallenge"
    )
    assert (
        labels["traefik.http.services.jv-ana-perez.loadbalancer.server.port"] == "8000"
    )
    assert labels["traefik.docker.network"] == "root_default"
    # The cookie regex matches only this slug, wherever it sits.
    rx = re.compile(r"(^|;\s*)jv=ana-perez(;|$)")
    assert rx.search("a=1; jv=ana-perez; b=2") and rx.search("jv=ana-perez")
    assert not rx.search("jv=ana-perez-2") and not rx.search("xjv=ana-perez")


def test_get_backend_selection(monkeypatch):
    monkeypatch.delenv("JARVIS_BACKEND", raising=False)
    assert isinstance(get_backend(), RailwayBackend)
    monkeypatch.setenv("JARVIS_BACKEND", "docker")
    assert isinstance(get_backend(), DockerBackend)
    rb = RailwayBackend(object())
    assert get_backend(rb) is rb


def test_instance_link(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://jarvis.aifasttrack.tech")
    monkeypatch.setenv("JARVIS_ROUTING", "cookie")
    assert instance_link("k") == "https://jarvis.aifasttrack.tech/entrar#key=k"
    monkeypatch.delenv("JARVIS_ROUTING")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "x.up.railway.app")
    assert instance_link("k") == "https://x.up.railway.app/#key=k"
    assert instance_link("") == ""


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class TestDockerBackend:
    def test_provision_creates_container(self, docker):
        fake, backend = docker
        result = provision_user(
            backend,
            name="Ana Pérez",
            email="ana@x.com",
            model="claude-haiku-5-5",
            shared_ai=True,
        )
        assert result["id"] == "ana-perez" and result["service"] == "jarvis-ana-perez"
        assert result["domain"] == "jarvis.aifasttrack.tech"
        assert result["link"].startswith(
            "https://jarvis.aifasttrack.tech/entrar#key=oj_sk_"
        )
        c = fake.containers["jarvis-ana-perez"]
        env = _env(c)
        assert env["FASTTRACK_ADMIN_TOKEN"] == "adm-token"  # from shared.env
        assert env["LEGAL_COMPANY"] == "Fast Factory IA Engine S.L."
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-1"
        assert env["JARVIS_ROUTING"] == "cookie"
        assert env["PUBLIC_URL"] == "https://jarvis.aifasttrack.tech"
        assert "RAILWAY_DOCKERFILE_PATH" not in env
        assert not any("${{" in v for v in env.values())
        labels = c["Config"]["Labels"]
        assert labels["fasttrack.jarvis"] == "instance"
        assert labels["fasttrack.jarvis.slug"] == "ana-perez"
        assert "ANTHROPIC_API_KEY" in labels["fasttrack.jarvis.shared"]
        assert "traefik.http.routers.jv-ana-perez.rule" in labels
        host = c["HostConfig"]
        assert host["NetworkMode"] == "root_default"
        assert host["RestartPolicy"] == {"Name": "unless-stopped"}
        assert host["Mounts"] == [
            {"Type": "volume", "Source": "jarvis-ana-perez-data", "Target": "/data"}
        ]
        assert "PortBindings" not in host
        assert "/health" in " ".join(c["body"]["Healthcheck"]["Test"])
        assert "jarvis-ana-perez-data" in fake.volumes
        assert c["State"]["Status"] == "running"
        # Same name again -> suffix.
        again = provision_user(backend, name="Ana Pérez", model="claude-haiku-5-5")
        assert again["id"] == "ana-perez-2"

    def test_services_and_status(self, docker):
        fake, backend = docker
        _adopt(backend, railway_id="rw-123")
        [svc] = backend.services()
        assert svc["id"] == "ana" and svc["name"] == "jarvis-ana"
        assert svc["deploy_status"] == "DEPLOYING"
        assert svc["aliases"] == ["rw-123"] and svc["volume_id"] == "jarvis-ana-data"
        fake.containers["jarvis-ana"]["status_text"] = "Up 2 hours (healthy)"
        assert backend.services()[0]["deploy_status"] == "SUCCESS"
        fake.containers["jarvis-ana"]["status_text"] = "Up 2 hours (unhealthy)"
        assert backend.services()[0]["deploy_status"] == "UNHEALTHY"
        fake.containers["jarvis-ana"]["State"]["Status"] = "exited"
        assert backend.services()[0]["deploy_status"] == "CRASHED"

    def test_railway_id_fallback(self, docker):
        _, backend = docker
        _adopt(backend, railway_id="7f3a-rw")
        assert backend.find("7f3a-rw")["id"] == "ana"
        assert backend.find("jarvis-ana")["id"] == "ana"
        assert backend.resolve_id("7f3a-rw") == "ana"
        assert backend.find("nope") is None
        with pytest.raises(DockerError):
            backend.resolve_id("nope")

    def test_existing_volume_needs_adoption(self, docker):
        fake, backend = docker
        fake.volumes["jarvis-ana-data"] = {"Name": "jarvis-ana-data"}
        with pytest.raises(DockerError, match="ya existe"):
            _adopt(backend, reuse=False)
        _adopt(backend, reuse=True)
        assert "jarvis-ana" in fake.containers
        with pytest.raises(DockerError, match="Ya existe"):
            _adopt(backend)

    def test_recreate_keeps_env_and_refreshes_shared(self, docker, shared_env):
        fake, backend = docker
        _adopt(backend, LEGAL_COMPANY="${{shared.LEGAL_COMPANY}}", TZ="Atlantic/Canary")
        shared_env.write_text("LEGAL_COMPANY=Nueva S.L.\n", encoding="utf-8")
        fake.images["openjarvis:latest"] = ["PATH=/new/bin", "HOME=/home/openjarvis"]
        backend.set_variables("ana", {"JARVIS_MODEL": "gpt-5"})
        c = fake.containers["jarvis-ana"]
        env = _env(c)
        assert env["OPENJARVIS_API_KEY"] == KEY  # kept
        assert env["JARVIS_MODEL"] == "gpt-5"  # updated
        assert env["LEGAL_COMPANY"] == "Nueva S.L."  # refreshed from shared
        assert env["TZ"] == "Atlantic/Canary"  # instance's own value kept
        assert env["PATH"] == "/new/bin"  # image ENV from the new image
        assert c["HostConfig"]["Mounts"][0]["Source"] == "jarvis-ana-data"
        assert c["State"]["Status"] == "running"
        # Stopped before being replaced (never two WhatsApp sessions).
        stop = fake.requests.index(("POST", "/v1.44/containers/jarvis-ana/stop"))
        create = max(
            i
            for i, r in enumerate(fake.requests)
            if r == ("POST", "/v1.44/containers/create")
        )
        assert stop < create

    def test_recreate_by_railway_id_keeps_label(self, docker):
        fake, backend = docker
        _adopt(backend, railway_id="rw-9")
        backend.recreate("rw-9")
        labels = fake.containers["jarvis-ana"]["Config"]["Labels"]
        assert labels[LABEL_RAILWAY] == "rw-9"

    def test_recreate_failure_restores_old(self, docker):
        fake, backend = docker
        _adopt(backend)
        fake.fail_create = True
        with pytest.raises(DockerError, match="boom"):
            backend.recreate("ana", {"JARVIS_MODEL": "x"})
        env = _env(fake.containers["jarvis-ana"])
        assert env["OPENJARVIS_API_KEY"] == KEY and "JARVIS_MODEL" not in env

    def test_delete(self, docker):
        fake, backend = docker
        _adopt(backend)
        backend.delete(backend.find("ana"))
        assert not fake.containers and not fake.volumes

    def test_endpoints(self, docker):
        _, backend = docker
        ends = backend.endpoints("ana", KEY)
        assert ends == {
            "host": "jarvis.aifasttrack.tech",
            "url": "http://jarvis-ana:8000",
            "link": f"https://jarvis.aifasttrack.tech/entrar#key={KEY}",
            "route": "ana",
        }

    def test_docker_unreachable(self, shared_env):
        def boom(request):
            raise httpx.ConnectError("refused")

        backend = DockerBackend(
            http=httpx.Client(transport=httpx.MockTransport(boom)),
            shared_env=str(shared_env),
        )
        with pytest.raises(DockerError, match="No se puede hablar con Docker"):
            backend.services()


# ---------------------------------------------------------------------------
# dockerctl
# ---------------------------------------------------------------------------


class TestDockerctl:
    def test_create_list_labels_delete(self, docker, tmp_path, capsys):
        fake, backend = docker
        fake.volumes["jarvis-pepe-data"] = {"Name": "jarvis-pepe-data"}  # copied
        env_file = tmp_path / "pepe.env"
        env_file.write_text(
            f"OPENJARVIS_API_KEY={KEY}\nJARVIS_ACCESS_CODE=PPPP-QQQQ-RRRR\n"
            "RAILWAY_SERVICE_ID=old\nJARVIS_USER_NAME=Pepe\n",
            encoding="utf-8",
        )
        rc = dockerctl.main(
            [
                "create",
                "--env-file",
                str(env_file),
                "--slug",
                "pepe",
                "--railway-id",
                "rw-pepe",
            ],
            backend=backend,
        )
        assert rc == 0
        env = _env(fake.containers["jarvis-pepe"])
        assert "RAILWAY_SERVICE_ID" not in env and env["JARVIS_USER_NAME"] == "Pepe"
        assert dockerctl.main(["list"], backend=backend) == 0
        assert dockerctl.main(["print-labels", "pepe"], backend=backend) == 0
        out = capsys.readouterr().out
        assert "rw-pepe" in out and "jv=pepe" in out
        assert (
            dockerctl.main(["set", "pepe", "JARVIS_MODEL=gpt-5"], backend=backend) == 0
        )
        assert _env(fake.containers["jarvis-pepe"])["JARVIS_MODEL"] == "gpt-5"
        assert dockerctl.main(["recreate", "--all"], backend=backend) == 0
        assert dockerctl.main(["delete", "pepe"], backend=backend) == 2
        assert "jarvis-pepe" in fake.containers
        assert (
            dockerctl.main(["delete", "pepe", "--confirm", "pepe"], backend=backend)
            == 0
        )
        assert not fake.containers and not fake.volumes

    def test_create_needs_key(self, docker, tmp_path):
        _, backend = docker
        env_file = tmp_path / "x.env"
        env_file.write_text("A=1\n", encoding="utf-8")
        assert (
            dockerctl.main(
                ["create", "--env-file", str(env_file), "--slug", "x"], backend=backend
            )
            == 2
        )


# ---------------------------------------------------------------------------
# Console, landing and billing on the Docker backend
# ---------------------------------------------------------------------------


@pytest.fixture
def console(docker, monkeypatch):
    fake, backend = docker
    _adopt(backend, railway_id="rw-1")
    seen = []

    def instance(request):
        seen.append(request)
        if request.url.path == "/api/whatsapp/status":
            return httpx.Response(200, json={"connected": True, "me": "34600"})
        if request.url.path == "/api/credits/topup":
            return httpx.Response(200, json={"remaining": 300})
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(instance))
    client = TestClient(create_admin_app(backend, password="pw", http=http))
    assert client.post("/api/login", json={"password": "pw"}).status_code == 200
    return client, fake, seen


H = {"X-Requested-With": "fasttrack"}


class TestConsoleDocker:
    def test_overview_uses_internal_url(self, console):
        client, _, seen = console
        data = client.get("/api/overview").json()
        assert data["backend"] == "docker" and data["railway"] is True
        assert data["credentials"]["ANTHROPIC_API_KEY"] is True
        [user] = data["users"]
        assert user["id"] == "ana" and user["aliases"] == ["rw-1"]
        assert user["link"] == f"https://jarvis.aifasttrack.tech/entrar#key={KEY}"
        assert user["whatsapp"] is True
        assert all(r.url.host == "jarvis-ana" for r in seen)

    def test_topup_and_railway_id(self, console, monkeypatch):
        client, _, seen = console
        monkeypatch.setenv("FASTTRACK_ADMIN_TOKEN", "adm")
        r = client.post("/api/users/rw-1/topup", headers=H)  # old Railway id
        assert r.status_code == 200, r.text
        assert str(seen[-1].url) == "http://jarvis-ana:8000/api/credits/topup"

    def test_rotate_and_delete(self, console):
        client, fake, _ = console
        r = client.post("/api/users/ana/rotate-key", headers=H)
        link = r.json()["link"]
        assert link.startswith("https://jarvis.aifasttrack.tech/entrar#key=oj_sk_")
        assert _env(fake.containers["jarvis-ana"])["OPENJARVIS_API_KEY"] in link
        bad = client.request(
            "DELETE", "/api/users/ana", json={"confirm": "otro"}, headers=H
        )
        assert bad.status_code == 400 and fake.containers
        ok = client.request(
            "DELETE", "/api/users/ana", json={"confirm": "ana"}, headers=H
        )
        assert ok.status_code == 200 and not fake.containers

    def test_create_user(self, console):
        client, fake, _ = console
        r = client.post("/api/users", json={"name": "Luis"}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == "luis" and "jarvis-luis" in fake.containers


class TestLandingDocker:
    @pytest.fixture
    def site(self, docker):
        _, backend = docker
        _adopt(backend)
        return TestClient(create_landing_app(backend), base_url="https://testserver")

    def test_access_sets_route_cookie(self, site):
        r = site.post("/api/acceder", json={"code": "abcd-efgh-jkmn"})
        assert r.status_code == 200
        assert r.json() == {"url": f"/#key={KEY}"}
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("jv=ana;")
        for attr in (
            "HttpOnly",
            "Path=/",
            "SameSite=lax",
            "Secure",
            "Max-Age=31536000",
        ):
            assert attr.lower() in cookie.lower()
        miss = site.post("/api/acceder", json={"code": "ZZZZ-ZZZZ-ZZZZ"})
        assert miss.status_code == 404 and "set-cookie" not in miss.headers

    def test_access_with_key(self, site):
        r = site.post("/api/acceder", json={"code": KEY})
        assert r.json()["url"] == f"/#key={KEY}"

    def test_salir_clears_cookie(self, site):
        r = site.get("/salir", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookies = r.headers.get_list("set-cookie")
        assert any(c.startswith('jv=""') or c.startswith("jv=;") for c in cookies)
        assert any("max-age=0" in c.lower() for c in cookies)

    def test_entrar_serves_site(self, site):
        r = site.get("/entrar")
        assert r.status_code == 200 and "/api/acceder" in r.text

    def test_oauth_callback_single_domain(self, site):
        from openjarvis.integrations.manager import encode_state

        state = encode_state("jarvis.aifasttrack.tech", "n1")
        r = site.get(
            "/oauth/callback",
            params={"state": state, "code": "c"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith(
            "https://jarvis.aifasttrack.tech/oauth/done?"
        )
        evil = encode_state("evil.example.com", "n1")
        r = site.get("/oauth/callback", params={"state": evil}, follow_redirects=False)
        assert r.status_code == 400


def test_billing_topup_with_railway_id(docker, monkeypatch):
    _, backend = docker
    _adopt(backend, railway_id="rw-ana")
    monkeypatch.setenv("FASTTRACK_ADMIN_TOKEN", "adm")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    ful = billing.Fulfillment(
        backend,
        lambda **kw: None,
        send=lambda m: None,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    event = {
        "id": "evt",
        "type": "checkout.session.completed",
        "data": {"object": {"metadata": {"kind": "topup", "service_id": "rw-ana"}}},
    }
    asyncio.run(ful._topup(event["data"]["object"], {"service_id": "rw-ana"}))
    assert str(seen[0].url) == "http://jarvis-ana:8000/api/credits/topup"
    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"


def test_instance_salir_route(monkeypatch):
    from fastapi import FastAPI

    from openjarvis.server.jarvis_ui_routes import router

    monkeypatch.setenv("PUBLIC_URL", "https://jarvis.aifasttrack.tech")
    app = FastAPI()
    app.include_router(router)
    r = TestClient(app).get("/salir", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://jarvis.aifasttrack.tech/"


def test_instance_shared_vars(docker, shared_env, monkeypatch):
    fake, backend = docker
    monkeypatch.setenv("JARVIS_INSTANCE_SHARED", "MAILJET_API_KEY, MS_CLIENT_ID")
    _adopt(backend)
    env = _env(fake.containers["jarvis-ana"])
    assert env["MAILJET_API_KEY"] == "mj"
    assert env["MS_CLIENT_ID"] == ""  # not in shared.env yet
    # Added to shared.env later: picked up by the next recreation.
    shared_env.write_text("MS_CLIENT_ID=ms-1\nMAILJET_API_KEY=mj2\n", encoding="utf-8")
    backend.recreate("ana")
    env = _env(fake.containers["jarvis-ana"])
    assert env["MS_CLIENT_ID"] == "ms-1" and env["MAILJET_API_KEY"] == "mj2"
