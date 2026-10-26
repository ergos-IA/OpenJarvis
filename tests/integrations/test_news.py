"""Tests for the news integration (RSS/Atom + Hacker News)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry
from openjarvis.integrations.providers import news

RSS = """<?xml version="1.0" encoding="ISO-8859-1"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<title>Portada</title>
<item><title><![CDATA[El Gobierno aprueba la ley de vivienda]]></title>
<link>https://elpais.com/a1</link><pubDate>Mon, 04 Mar 2030 09:00:00 GMT</pubDate>
<description><![CDATA[<p>Texto <b>largo</b></p>]]></description></item>
<item><title>La bolsa sube un 2%</title><link>https://elpais.com/a2</link>
<pubDate>Mon, 04 Mar 2030 11:30:00 +0100</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Mi blog</title>
<entry><title>Nuevo artículo sobre vivienda</title>
<link rel="alternate" href="https://blog.ejemplo.es/post-1"/>
<updated>2030-03-04T12:00:00Z</updated><summary>Resumen</summary></entry>
</feed>"""

BOMB = """<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title></item></channel></rss>"""


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Madrid")
    monkeypatch.setattr("openjarvis.security.ssrf.check_ssrf", lambda url: None)
    holder = SimpleNamespace(handler=lambda r: httpx.Response(404), calls=[])

    def dispatch(request):
        holder.calls.append(request)
        return holder.handler(request)

    base.set_http(httpx.Client(transport=httpx.MockTransport(dispatch)))
    news._CACHE.clear()
    yield holder
    base.set_http(None)
    news._CACHE.clear()


def tools_of(creds=None):
    return {t.spec.name: t for t in registry()["news"].build_tools(creds or {})}


def test_registered_always_on_with_optional_feeds():
    integ = registry()["news"]
    assert integ.always_on and integ.category == "personal"
    assert integ.fields[0].key == "feeds" and integ.fields[0].optional
    assert set(tools_of()) == {"news_headlines", "news_sources"}


def test_parse_rss_and_atom():
    items = news.parse_feed(RSS, "El País")
    assert [i["titular"] for i in items] == [
        "El Gobierno aprueba la ley de vivienda",
        "La bolsa sube un 2%",
    ]
    assert items[0]["resumen"] == "Texto largo"
    assert (
        items[0]["fecha"].hour == 9
        and items[1]["fecha"].utcoffset().total_seconds() == 3600
    )
    atom = news.parse_feed(ATOM)
    assert atom[0]["fuente"] == "Mi blog"
    assert atom[0]["enlace"] == "https://blog.ejemplo.es/post-1"
    assert atom[0]["fecha"].isoformat() == "2030-03-04T12:00:00+00:00"


def test_parse_refuses_dtd_and_html():
    with pytest.raises(IntegrationError, match="RSS"):
        news.parse_feed(BOMB)
    with pytest.raises(IntegrationError, match="RSS"):
        news.parse_feed("<!doctype html><html><body>hola</body></html>")
    with pytest.raises(IntegrationError, match="RSS"):
        news.parse_feed("<html><body>hola</body></html>")
    with pytest.raises(IntegrationError, match="RSS"):
        news.parse_feed("esto no es xml")


def test_default_headlines_and_cache(api):
    api.handler = lambda r: httpx.Response(
        200, text=RSS, headers={"content-type": "application/rss+xml"}
    )
    t = tools_of()["news_headlines"]
    out = t.execute().content
    hosts = [r.url.host for r in api.calls]
    assert hosts == [
        "feeds.elpais.com",
        "api2.rtve.es",
        "e00-elmundo.uecdn.es",
        "e00-expansion.uecdn.es",
    ]
    # Same headline in every mocked feed: shown once, newest first.
    lines = [x for x in out.splitlines() if x.startswith("- ")]
    assert lines == [
        "- La bolsa sube un 2% (lun 04/03 11:30 · El País)",
        "- El Gobierno aprueba la ley de vivienda (lun 04/03 10:00 · El País)",
    ]
    assert "https://elpais.com/a2" in out

    t.execute(buscar="VIVIENDA")
    assert len(api.calls) == 4  # served from the 10-minute cache
    out = t.execute(buscar="vivienda").content
    assert "bolsa" not in out and "ley de vivienda" in out
    assert (
        "No he encontrado titulares sobre «fútbol»"
        in t.execute(buscar="fútbol").content
    )


def test_source_selection_and_user_feeds(api):
    def handler(req):
        if req.url.host == "blog.ejemplo.es":
            return httpx.Response(200, text=ATOM)
        if req.url.host == "roto.example.com":
            return httpx.Response(500)
        return httpx.Response(200, text=RSS)

    api.handler = handler
    creds = {
        "feeds": "https://blog.ejemplo.es/feed\nhttps://roto.example.com/rss  ftp://x"
    }
    t = tools_of(creds)["news_headlines"]

    t.execute(fuente="Expansión")
    assert api.calls[-1].url.host == "e00-expansion.uecdn.es"

    out = t.execute(fuente="mías").content
    assert "Nuevo artículo sobre vivienda (lun 04/03 13:00 · Mi blog)" in out
    assert "(No he podido leer: roto.example.com)" in out

    out = t.execute(buscar="vivienda").content  # general + own feeds
    assert "Mi blog" in out and "El País" in out

    bad = t.execute(fuente="la gaceta de marte")
    assert not bad.success and "news_sources" in bad.content

    sources = tools_of(creds)["news_sources"].execute().content
    assert "expansion: Expansión" in sources and "blog.ejemplo.es" in sources
    assert "no ha añadido" in tools_of()["news_sources"].execute().content


def test_hacker_news(api):
    def handler(req):
        if req.url.path == "/v0/topstories.json":
            return httpx.Response(200, json=[11, 22, 33])
        sid = int(req.url.path.rsplit("/", 1)[-1].split(".")[0])
        story = {
            11: {
                "id": 11,
                "title": "Show HN: Jarvis",
                "url": "https://x.dev",
                "score": 120,
                "descendants": 30,
                "time": 1899000000,
            },
            22: {"id": 22, "title": "Ask HN: Python?", "score": 50, "time": 1899000100},
            33: {"id": 33, "deleted": True},
        }[sid]
        return httpx.Response(200, json=story)

    api.handler = handler
    out = tools_of()["news_headlines"].execute(fuente="Hacker News", limite=3).content
    assert "Ask HN: Python?" in out and "https://news.ycombinator.com/item?id=22" in out
    assert "Show HN: Jarvis" in out and "https://x.dev" in out
    assert out.index("Ask HN") < out.index("Show HN")  # newest first
    assert all(r.url.host == "hacker-news.firebaseio.com" for r in api.calls)
    assert len(api.calls) == 4


def test_check(api):
    integ = registry()["news"]
    assert integ.check({}) == "Fuentes por defecto"
    api.handler = lambda r: httpx.Response(200, text=ATOM)
    assert (
        integ.check({"feeds": "https://blog.ejemplo.es/feed"})
        == "1 fuente(s) propia(s)"
    )
    with pytest.raises(IntegrationError, match="https://"):
        integ.check({"feeds": "blog.ejemplo.es/feed"})
    api.handler = lambda r: httpx.Response(200, text="<html>no</html>")
    with pytest.raises(IntegrationError, match="no es un feed"):
        integ.check({"feeds": "https://blog.ejemplo.es/otro"})
