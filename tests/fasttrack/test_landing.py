"""Tests for the public product site and access-code routing."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from starlette.testclient import TestClient  # noqa: E402

from openjarvis.fasttrack.admin import new_access_code, normalize_code  # noqa: E402
from openjarvis.fasttrack.landing import Directory, create_landing_app  # noqa: E402
from openjarvis.server.web_session import FailureLimiter  # noqa: E402

KEY = "oj_sk_" + "a" * 43


class FakeRailway:
    configured = True

    def __init__(self):
        self.calls = 0
        self.services = [
            {"id": "S1", "name": "jarvis-ana"},
            {"id": "C", "name": "consola-aifasttrack"},
        ]
        self.vars = {
            "S1": {"OPENJARVIS_API_KEY": KEY, "JARVIS_ACCESS_CODE": "ABCD-EFGH-JKMN"},
            "C": {"OPENJARVIS_API_KEY": "oj_sk_" + "c" * 43},
        }

    def project_overview(self):
        self.calls += 1
        return {"services": self.services}

    def variables(self, sid):
        return self.vars.get(sid, {})

    def domain(self, sid):
        return f"{sid.lower()}.up.railway.app"


def test_access_code_format():
    code = new_access_code()
    assert len(code) == 14 and code.count("-") == 2
    assert not set(normalize_code(code)) & set("01IOL")


class TestDirectory:
    def test_resolve_code_key_and_link(self):
        d = Directory(FakeRailway())
        expected = f"https://s1.up.railway.app/#key={KEY}"
        assert d.resolve("abcd efgh jkmn") == expected
        assert d.resolve(KEY) == expected
        assert d.resolve(f"https://s1.up.railway.app/#key={KEY}") == expected

    def test_non_user_services_ignored(self):
        assert Directory(FakeRailway()).resolve("oj_sk_" + "c" * 43) is None

    def test_wrong_or_malformed(self):
        d = Directory(FakeRailway())
        assert d.resolve("ABCD-EFGH-JKMP") is None
        assert d.resolve("short") is None

    def test_cache_and_refresh_on_miss(self):
        rw = FakeRailway()
        d = Directory(rw, ttl_s=300, min_gap_s=0)
        d.resolve("ABCD-EFGH-JKMN")
        d.resolve("ABCD-EFGH-JKMN")
        assert rw.calls == 1  # cached
        rw.services.append({"id": "S2", "name": "jarvis-new"})
        rw.vars["S2"] = {
            "OPENJARVIS_API_KEY": KEY + "x",
            "JARVIS_ACCESS_CODE": "PPPP-QQQQ-RRRR",
        }
        assert d.resolve("PPPP-QQQQ-RRRR") == f"https://s2.up.railway.app/#key={KEY}x"


@pytest.fixture
def client():
    app = create_landing_app(
        FakeRailway(),
        per_ip=FailureLimiter(max_failures=3, window_s=600, block_s=600),
    )
    return TestClient(app)


class TestSite:
    def test_pages(self, client):
        home = client.get("/")
        assert home.status_code == 200 and "Acceder a mi Jarvis" in home.text
        assert "{{CONTACT_URL}}" not in home.text
        assert client.get("/legal/terminos").status_code == 200
        assert client.get("/jarvis-ui/inter.woff2").status_code == 200
        assert client.get("/health").json() == {"status": "ok"}
        assert "frame-ancestors 'none'" in home.headers["content-security-policy"]

    def test_access_ok(self, client):
        r = client.post("/api/acceder", json={"code": "abcd-efgh-jkmn"})
        assert r.status_code == 200
        assert r.json()["url"] == f"https://s1.up.railway.app/#key={KEY}"

    def test_brute_force_blocked(self, client):
        h = {"X-Real-IP": "7.7.7.7"}
        for _ in range(3):
            assert (
                client.post(
                    "/api/acceder", json={"code": "ZZZZ-ZZZZ-ZZZZ"}, headers=h
                ).status_code
                == 404
            )
        assert (
            client.post(
                "/api/acceder", json={"code": "ABCD-EFGH-JKMN"}, headers=h
            ).status_code
            == 429
        )
        other = {"X-Real-IP": "8.8.8.8"}
        assert (
            client.post(
                "/api/acceder", json={"code": "ABCD-EFGH-JKMN"}, headers=other
            ).status_code
            == 200
        )
