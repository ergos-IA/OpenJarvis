# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Spotify: what the user listens to, and simple playback control, via OAuth.

Playback control (play, pause, next) needs Spotify Premium and an open
Spotify app on some device; both cases get a friendly Spanish answer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from openjarvis.integrations.base import (
    I,
    Integration,
    IntegrationError,
    OAuthSpec,
    S,
    http,
    pick,
    register,
    request,
    tool,
)

API = "https://api.spotify.com/v1"
AUTH_ERROR = (
    "Spotify no me deja entrar: el acceso ha caducado o tu cuenta no está "
    "autorizada en la app de Jarvis. Vuelve a conectar Spotify o escríbenos."
)
PREMIUM_ERROR = (
    "Spotify solo deja controlar la música (poner, pausar, pasar canción) con "
    "una cuenta Premium. Sí puedo decirte qué estás escuchando y tus favoritos."
)
NO_DEVICE_ERROR = (
    "No veo ningún Spotify abierto. Abre Spotify en tu móvil, ordenador o "
    "altavoz, pon cualquier canción un segundo y vuelve a pedírmelo."
)

_KINDS = {
    "cancion": "track",
    "canción": "track",
    "track": "track",
    "playlist": "playlist",
    "lista": "playlist",
    "album": "album",
    "álbum": "album",
    "artista": "artist",
    "artist": "artist",
    "podcast": "show",
}


def _headers(creds: Any) -> Dict[str, str]:
    return {"Authorization": f"Bearer {creds.token()}"}


def _get(creds: Any, path: str, **params: Any) -> Any:
    return request(
        "GET",
        f"{API}{path}",
        headers=_headers(creds),
        params=params or None,
        auth_error=AUTH_ERROR,
    )


def _player(
    creds: Any, method: str, path: str, json: Optional[Dict[str, Any]] = None
) -> None:
    """Playback commands: map Spotify's 403/404 to friendly messages."""
    resp = http().request(
        method, f"{API}/me/player{path}", headers=_headers(creds), json=json
    )
    if resp.status_code in (200, 202, 204):
        return
    reason = ""
    try:
        reason = (resp.json().get("error") or {}).get("reason", "")
    except Exception:
        pass
    if resp.status_code == 401:
        raise IntegrationError(AUTH_ERROR)
    if resp.status_code == 403:
        if reason and reason != "PREMIUM_REQUIRED":
            raise IntegrationError(
                "Spotify no ha permitido esa orden ahora mismo (quizá ya estaba "
                "en pausa o no hay siguiente canción)."
            )
        raise IntegrationError(PREMIUM_ERROR)
    if resp.status_code == 404:
        raise IntegrationError(NO_DEVICE_ERROR)
    if resp.status_code == 429:
        raise IntegrationError("Spotify pide esperar un poco (límite de uso).")
    raise IntegrationError(f"Spotify respondió {resp.status_code}: {resp.text[:200]}")


def _artists(item: Dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists", []))


def _track(t: Dict[str, Any]) -> Dict[str, Any]:
    return pick(
        {
            "cancion": t.get("name", ""),
            "artista": _artists(t),
            "album": (t.get("album") or {}).get("name", ""),
        },
        "cancion",
        "artista",
        "album",
    )


def _ago(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    mins = int((datetime.now(timezone.utc) - then).total_seconds() // 60)
    if mins < 60:
        return f"hace {max(mins, 1)} min"
    if mins < 60 * 24:
        return f"hace {mins // 60} h"
    return f"hace {mins // (60 * 24)} días"


def _check(creds: Any) -> str:
    me = _get(creds, "/me") or {}
    return me.get("display_name") or me.get("id") or "Spotify"


def _tools(creds: Any):
    @tool(
        "spotify_now_playing",
        "Dice qué está sonando ahora mismo en el Spotify del usuario y en qué dispositivo.",
        integration="spotify",
    )
    def spotify_now_playing() -> Any:
        data = _get(creds, "/me/player")
        if not data or not data.get("item"):
            return "Ahora mismo no está sonando nada en Spotify."
        item = data["item"]
        if item.get("type") == "episode":
            out = {
                "episodio": item.get("name", ""),
                "podcast": (item.get("show") or {}).get("name", ""),
            }
        else:
            out = _track(item)
        out["estado"] = "sonando" if data.get("is_playing") else "en pausa"
        device = data.get("device") or {}
        if device.get("name"):
            out["dispositivo"] = device["name"]
        return out

    @tool(
        "spotify_recently_played",
        "Últimas canciones que ha escuchado el usuario en Spotify.",
        {"cantidad": I("Cuántas (1-50, por defecto 10)")},
        integration="spotify",
    )
    def spotify_recently_played(cantidad: int = 10) -> Any:
        data = _get(
            creds,
            "/me/player/recently-played",
            limit=max(1, min(int(cantidad or 10), 50)),
        )
        items = (data or {}).get("items", [])
        if not items:
            return "No hay canciones recientes en Spotify."
        return [
            {**_track(i.get("track") or {}), "cuando": _ago(i.get("played_at", ""))}
            for i in items
        ]

    @tool(
        "spotify_top",
        "Artistas o canciones favoritos del usuario en Spotify (lo que más escucha).",
        {
            "tipo": S("«artistas» o «canciones»", enum=["artistas", "canciones"]),
            "periodo": S(
                "«mes» (últimas 4 semanas), «medio» (6 meses) o «siempre» (unos años)",
                enum=["mes", "medio", "siempre"],
            ),
            "cantidad": I("Cuántos (1-20, por defecto 10)"),
        },
        integration="spotify",
    )
    def spotify_top(
        tipo: str = "artistas", periodo: str = "medio", cantidad: int = 10
    ) -> Any:
        kind = "tracks" if tipo.startswith("canc") else "artists"
        rng = {"mes": "short_term", "siempre": "long_term"}.get(periodo, "medium_term")
        data = _get(
            creds,
            f"/me/top/{kind}",
            time_range=rng,
            limit=max(1, min(int(cantidad or 10), 20)),
        )
        items = (data or {}).get("items", [])
        if not items:
            return "Spotify aún no tiene suficientes datos de lo que escuchas."
        if kind == "tracks":
            return [_track(t) for t in items]
        return [
            pick(
                {
                    "artista": a.get("name", ""),
                    "generos": ", ".join(a.get("genres", [])[:3]),
                },
                "artista",
                "generos",
            )
            for a in items
        ]

    @tool(
        "spotify_play",
        "Busca una canción, playlist, álbum o artista en Spotify y lo pone en el "
        "dispositivo donde el usuario tiene Spotify abierto (requiere Premium). "
        "Sin texto, reanuda la música.",
        {
            "buscar": S(
                "Qué poner, p. ej. «Bohemian Rhapsody Queen» o «Éxitos de los 80»"
            ),
            "tipo": S(
                "cancion, playlist, album o artista (por defecto cancion)",
                enum=["cancion", "playlist", "album", "artista"],
            ),
        },
        # Not marked writes=True: playing music changes no stored data, and a
        # "¿seguro?" before every song would be annoying.
        integration="spotify",
    )
    def spotify_play(buscar: str = "", tipo: str = "cancion") -> str:
        if not buscar:
            _player(creds, "PUT", "/play")
            return "Música reanudada."
        kind = _KINDS.get((tipo or "cancion").lower(), "track")
        # Development-mode apps: search limit is 10 at most.
        data = _get(creds, "/search", q=buscar, type=kind, limit=5)
        items = [i for i in ((data or {}).get(f"{kind}s") or {}).get("items", []) if i]
        if not items:
            raise IntegrationError(f"No encuentro «{buscar}» en Spotify.")
        item = items[0]
        if kind == "track":
            _player(creds, "PUT", "/play", {"uris": [item["uri"]]})
            return f"Sonando: {item.get('name', '')} — {_artists(item)}"
        _player(creds, "PUT", "/play", {"context_uri": item["uri"]})
        label = {"playlist": "la playlist", "album": "el álbum", "artist": "a"}.get(
            kind, ""
        )
        return f"Sonando {label} {item.get('name', '')}".replace("  ", " ")

    @tool(
        "spotify_pause",
        "Pausa la música de Spotify del usuario (requiere Premium).",
        integration="spotify",
    )
    def spotify_pause() -> str:
        _player(creds, "PUT", "/pause")
        return "Música en pausa."

    @tool(
        "spotify_next",
        "Pasa a la siguiente canción en Spotify (requiere Premium).",
        integration="spotify",
    )
    def spotify_next() -> str:
        _player(creds, "POST", "/next")
        return "Siguiente canción."

    return [
        spotify_now_playing,
        spotify_recently_played,
        spotify_top,
        spotify_play,
        spotify_pause,
        spotify_next,
    ]


register(
    Integration(
        id="spotify",
        name="Spotify",
        category="personal",
        description="Qué escuchas, tus favoritos y poner música desde WhatsApp.",
        abilities=[
            "¿Qué canción está sonando?",
            "Pon «Despacito» en Spotify",
            "¿Cuáles son mis artistas favoritos este mes?",
        ],
        steps=[
            "Pulsa el botón «<b>Conectar con Spotify</b>».",
            "Entra con tu cuenta de Spotify y pulsa «<b>Aceptar</b>».",
            "¡Listo! Para que Jarvis ponga, pause o pase canciones necesitas Spotify <b>Premium</b> y tener Spotify abierto en algún dispositivo (móvil, ordenador o altavoz).",
            "Si Spotify te dice que no tienes acceso, escríbenos: tenemos que darte de alta en la app de Jarvis.",
        ],
        oauth=OAuthSpec(
            provider="spotify",
            authorize_url="https://accounts.spotify.com/authorize",
            token_url="https://accounts.spotify.com/api/token",
            scopes=[
                "user-read-recently-played",
                "user-read-currently-playing",
                "user-read-playback-state",
                "user-modify-playback-state",
                "playlist-read-private",
                "user-top-read",
            ],
            client_env="SPOTIFY",
            basic_auth=True,
        ),
        check=_check,
        build_tools=_tools,
        prompt="Herramientas spotify_*: qué suena (spotify_now_playing), recientes (spotify_recently_played), favoritos (spotify_top), poner música (spotify_play), pausar (spotify_pause) y siguiente (spotify_next); controlar la música requiere Premium.",
        icon="♫",
        color="#1DB954",
        link="https://open.spotify.com",
    )
)
