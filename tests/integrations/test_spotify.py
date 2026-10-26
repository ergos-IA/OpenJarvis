"""Tests for the Spotify OAuth integration."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from openjarvis.integrations import base
from openjarvis.integrations.base import IntegrationError, registry
from openjarvis.integrations.manager import Creds, IntegrationManager


@pytest.fixture
def api():
    holder = SimpleNamespace(handler=lambda r: httpx.Response(404), calls=[])

    def dispatch(request):
        holder.calls.append(request)
        return holder.handler(request)

    base.set_http(httpx.Client(transport=httpx.MockTransport(dispatch)))
    yield holder
    base.set_http(None)


CREDS = Creds({}, lambda: "tok")


def tools():
    return {t.spec.name: t for t in registry()["spotify"].build_tools(CREDS)}


TRACK = {
    "type": "track",
    "name": "Bohemian Rhapsody",
    "uri": "spotify:track:1",
    "artists": [{"name": "Queen"}],
    "album": {"name": "A Night at the Opera"},
}


def test_oauth_spec(tmp_path, monkeypatch):
    spec = registry()["spotify"].oauth
    assert spec.basic_auth and spec.client_env == "SPOTIFY"
    assert set(spec.scopes) == {
        "user-read-recently-played",
        "user-read-currently-playing",
        "user-read-playback-state",
        "user-modify-playback-state",
        "playlist-read-private",
        "user-top-read",
    }
    monkeypatch.setenv("OPENJARVIS_SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("OPENJARVIS_SPOTIFY_CLIENT_SECRET", "s")
    url = IntegrationManager(tmp_path / "i.json").begin_oauth("spotify", "h")
    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert "user-top-read" in parse_qs(urlparse(url).query)["scope"][0].split(" ")


def test_check_and_401(api):
    def handler(r):
        assert r.headers["Authorization"] == "Bearer tok" and r.url.path == "/v1/me"
        return httpx.Response(200, json={"display_name": "Ana", "id": "ana1"})

    api.handler = handler
    assert registry()["spotify"].check(CREDS) == "Ana"
    api.handler = lambda r: httpx.Response(401, json={})
    with pytest.raises(IntegrationError, match="Spotify"):
        registry()["spotify"].check(CREDS)
    res = tools()["spotify_pause"].execute()
    assert not res.success and "Vuelve a conectar Spotify" in res.content


def test_now_playing(api):
    api.handler = lambda r: httpx.Response(
        200, json={"is_playing": True, "item": TRACK, "device": {"name": "Salón"}}
    )
    out = json.loads(tools()["spotify_now_playing"].execute().content)
    assert out == {
        "cancion": "Bohemian Rhapsody",
        "artista": "Queen",
        "album": "A Night at the Opera",
        "estado": "sonando",
        "dispositivo": "Salón",
    }
    api.handler = lambda r: httpx.Response(204)
    assert "no está sonando" in tools()["spotify_now_playing"].execute().content


def test_recent_and_top(api):
    def handler(r):
        params = parse_qs(r.url.query.decode())
        if r.url.path == "/v1/me/player/recently-played":
            assert params["limit"] == ["3"]
            return httpx.Response(
                200,
                json={"items": [{"track": TRACK, "played_at": "2001-01-01T00:00:00Z"}]},
            )
        if r.url.path == "/v1/me/top/artists":
            assert params["time_range"] == ["short_term"]
            return httpx.Response(
                200,
                json={"items": [{"name": "Rosalía", "genres": ["pop", "flamenco"]}]},
            )
        if r.url.path == "/v1/me/top/tracks":
            assert params["time_range"] == ["medium_term"]
            return httpx.Response(200, json={"items": [TRACK]})
        return httpx.Response(404)

    api.handler = handler
    t = tools()
    recent = json.loads(t["spotify_recently_played"].execute(cantidad=3).content)
    assert recent[0]["cancion"] == "Bohemian Rhapsody" and "días" in recent[0]["cuando"]
    top = json.loads(t["spotify_top"].execute(tipo="artistas", periodo="mes").content)
    assert top == [{"artista": "Rosalía", "generos": "pop, flamenco"}]
    songs = json.loads(t["spotify_top"].execute(tipo="canciones").content)
    assert songs[0]["artista"] == "Queen"


def test_play_track_and_playlist(api):
    played = []

    def handler(r):
        if r.url.path == "/v1/search":
            params = parse_qs(r.url.query.decode())
            assert int(params["limit"][0]) <= 10
            kind = params["type"][0]
            if kind == "track":
                return httpx.Response(200, json={"tracks": {"items": [TRACK]}})
            return httpx.Response(
                200,
                json={
                    "playlists": {
                        "items": [
                            None,
                            {"name": "Éxitos 80", "uri": "spotify:playlist:9"},
                        ]
                    }
                },
            )
        if r.url.path == "/v1/me/player/play":
            assert r.method == "PUT"
            played.append(json.loads(r.content) if r.content else None)
            return httpx.Response(204)
        return httpx.Response(404)

    api.handler = handler
    t = tools()
    out = t["spotify_play"].execute(buscar="bohemian")
    assert out.content == "Sonando: Bohemian Rhapsody — Queen"
    out = t["spotify_play"].execute(buscar="80", tipo="playlist")
    assert out.content == "Sonando la playlist Éxitos 80"
    assert t["spotify_play"].execute().content == "Música reanudada."
    assert played == [
        {"uris": ["spotify:track:1"]},
        {"context_uri": "spotify:playlist:9"},
        None,
    ]


def test_playback_errors(api):
    t = tools()
    api.handler = lambda r: httpx.Response(
        403, json={"error": {"status": 403, "reason": "PREMIUM_REQUIRED"}}
    )
    res = t["spotify_pause"].execute()
    assert not res.success and "Premium" in res.content
    api.handler = lambda r: httpx.Response(
        404, json={"error": {"status": 404, "reason": "NO_ACTIVE_DEVICE"}}
    )
    res = t["spotify_next"].execute()
    assert not res.success and "Abre Spotify" in res.content
    api.handler = lambda r: httpx.Response(
        403, json={"error": {"status": 403, "reason": "UNKNOWN"}}
    )
    assert "no ha permitido" in t["spotify_next"].execute().content
    calls = [c for c in api.calls if c.url.path == "/v1/me/player/next"]
    assert calls and calls[0].method == "POST"
