# ruff: noqa: E501  (user-facing guide texts are long prose)
"""News headlines: curated Spanish RSS feeds, Hacker News and the user's own feeds.

Always on (no connection needed).  Optionally the user pastes extra RSS/Atom
addresses; while not connected the tools are built with empty credentials.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from openjarvis.integrations.base import (
    Field,
    I,
    Integration,
    IntegrationError,
    S,
    fetch_public,
    register,
    request,
    tool,
)

# key -> (name, url).  Checked to answer with RSS 2.0 (Oct 2026).
DEFAULT_FEEDS: Dict[str, Tuple[str, str]] = {
    "elpais": (
        "El País",
        "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada",
    ),
    "rtve": ("RTVE", "https://api2.rtve.es/rss/temas_noticias.xml"),
    "elmundo": ("El Mundo", "https://e00-elmundo.uecdn.es/elmundo/rss/portada.xml"),
    "expansion": ("Expansión", "https://e00-expansion.uecdn.es/rss/portada.xml"),
    "eldiario": ("elDiario.es", "https://www.eldiario.es/rss/"),
    "abc": ("ABC", "https://www.abc.es/rss/2.0/portada/"),
    "lavanguardia": ("La Vanguardia", "https://www.lavanguardia.com/rss/home.xml"),
    "deportes": (
        "Deportes (El Mundo)",
        "https://e00-elmundo.uecdn.es/elmundodeporte/rss/portada.xml",
    ),
}
# What «las noticias» means when no source is named.
GENERAL = ["elpais", "rtve", "elmundo", "expansion"]

HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"

CACHE_S = 600
_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_DAYS = "lun mar mié jue vie sáb dom".split()


def _local(dt: datetime) -> datetime:
    from openjarvis.personal.reminders import local_tz

    return dt.astimezone(local_tz())


def _fmt_when(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    d = _local(dt)
    return f"{_DAYS[d.weekday()]} {d:%d/%m %H:%M}"


def _date(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)  # RSS: RFC 822
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom: RFC 3339
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local_name(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in el:
        if _local_name(c.tag) == name:
            return c
    return None


def _text(el: ET.Element, *names: str) -> str:
    for name in names:
        c = _child(el, name)
        if c is not None:
            value = "".join(c.itertext()).strip()
            if value:
                return value
    return ""


def _clean(text: str, limit: int = 220) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def parse_feed(text: str, source: str = "") -> List[Dict[str, Any]]:
    """Items of an RSS 2.0 / RSS 1.0 / Atom document: title, link, date, summary."""
    head = text[:5000].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in text.upper():
        # No DTDs: avoids entity-expansion attacks (and HTML pages).
        raise IntegrationError("Ese enlace no es un feed RSS/Atom válido.")
    try:
        # Already decoded text: drop the declaration (its encoding no longer applies).
        body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿"))
        root = ET.fromstring(body.strip())
    except ET.ParseError:
        raise IntegrationError("Ese enlace no es un feed RSS/Atom válido.")
    kind = _local_name(root.tag)
    if kind not in ("rss", "feed", "rdf"):
        raise IntegrationError("Ese enlace no es un feed RSS/Atom válido.")
    if not source:
        channel = _child(root, "channel")
        source = _text(channel if channel is not None else root, "title") or ""
    items = []
    for el in root.iter():
        name = _local_name(el.tag)
        if name not in ("item", "entry"):
            continue
        title = _clean(_text(el, "title"), 300)
        if not title:
            continue
        link = ""
        if name == "entry":
            for c in el:
                if (
                    _local_name(c.tag) == "link"
                    and c.get("rel", "alternate") == "alternate"
                ):
                    link = c.get("href", "")
                    break
        link = link or _text(el, "link") or _text(el, "guid")
        items.append(
            {
                "fuente": source,
                "titular": title,
                "enlace": link,
                "fecha": _date(_text(el, "pubdate", "published", "updated", "date")),
                "resumen": _clean(_text(el, "description", "summary", "content")),
            }
        )
    return items


def _feed(url: str, source: str) -> List[Dict[str, Any]]:
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    items = parse_feed(fetch_public(url), source)
    _CACHE[url] = (time.time(), items)
    return items


def _hacker_news(n: int) -> List[Dict[str, Any]]:
    key = f"hn:{n}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    ids = request("GET", HN_TOP) or []
    items = []
    for story_id in ids[:n]:
        story = request("GET", HN_ITEM.format(int(story_id))) or {}
        if not story.get("title"):
            continue
        items.append(
            {
                "fuente": "Hacker News",
                "titular": story["title"],
                "enlace": story.get("url")
                or f"https://news.ycombinator.com/item?id={story_id}",
                "fecha": datetime.fromtimestamp(story["time"], timezone.utc)
                if story.get("time")
                else None,
                "resumen": f"{story.get('score', 0)} puntos, {story.get('descendants', 0)} comentarios",
            }
        )
    _CACHE[key] = (time.time(), items)
    return items


def _user_feeds(creds: Dict[str, Any]) -> List[str]:
    raw = str((creds or {}).get("feeds", "") or "")
    return [
        u
        for u in re.split(r"[\s,]+", raw)
        if u.lower().startswith(("http://", "https://"))
    ][:15]


def _host(url: str) -> str:
    return (urlparse(url).hostname or url).removeprefix("www.")


def _check(creds: Dict[str, Any]) -> str:
    urls = _user_feeds(creds)
    if not urls:
        if (creds or {}).get("feeds"):
            raise IntegrationError(
                "No veo ningún enlace válido: deben empezar por https://"
            )
        return "Fuentes por defecto"
    for url in urls:
        _CACHE.pop(url, None)
        try:
            _feed(url, "")
        except IntegrationError as exc:
            raise IntegrationError(f"{url}: {exc}")
    return f"{len(urls)} fuente(s) propia(s)"


def _tools(creds: Dict[str, Any]):
    own = _user_feeds(creds)

    def sources_for(fuente: str) -> List[Tuple[str, str]]:
        """[(name, url)] — 'hn' as url means Hacker News."""
        f = (fuente or "").strip().lower()
        if not f:
            return [DEFAULT_FEEDS[k] for k in GENERAL] + [("", u) for u in own]
        if f in ("todas", "todo", "all"):
            return list(DEFAULT_FEEDS.values()) + [("", u) for u in own]
        if f.replace(" ", "") in ("hn", "hackernews", "tecnologia", "tecnología"):
            return [("Hacker News", "hn")]
        if f in ("mias", "mías", "propias", "mis fuentes"):
            return [("", u) for u in own]
        key = re.sub(
            r"[^a-z]",
            "",
            f.replace("ñ", "n").replace("á", "a").replace("é", "e").replace("ó", "o"),
        )
        if key in DEFAULT_FEEDS:
            return [DEFAULT_FEEDS[key]]
        for name, url in DEFAULT_FEEDS.values():
            if f in name.lower():
                return [(name, url)]
        mine = [("", u) for u in own if f in u.lower()]
        if mine:
            return mine
        raise IntegrationError(
            f"No conozco la fuente «{fuente}». Usa news_sources para ver las disponibles."
        )

    @tool(
        "news_headlines",
        "Últimos titulares de noticias. Por defecto, de la prensa española "
        "(El País, RTVE, El Mundo, Expansión) más las fuentes propias del usuario. "
        "Se puede elegir una fuente (p. ej. «elpais», «expansion», «deportes», "
        "«hackernews», «mías», «todas») y filtrar por palabra clave.",
        {
            "fuente": S("Fuente concreta (opcional; ver news_sources)"),
            "buscar": S(
                "Palabra clave para filtrar titulares, p. ej. «vivienda» (opcional)"
            ),
            "limite": I("Máximo de titulares (1-30, por defecto 10)"),
        },
        integration="news",
    )
    def news_headlines(fuente: str = "", buscar: str = "", limite: int = 10) -> str:
        limit = max(1, min(int(limite or 10), 30))
        items: List[Dict[str, Any]] = []
        failed = []
        for name, url in sources_for(fuente):
            try:
                if url == "hn":
                    items.extend(_hacker_news(30 if buscar else limit))
                else:
                    items.extend(_feed(url, name))
            except Exception:
                failed.append(name or _host(url))
        if buscar:
            q = buscar.lower()
            items = [
                i
                for i in items
                if q in f"{i['titular']} {i.get('resumen', '')}".lower()
            ]
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        items.sort(key=lambda i: i.get("fecha") or epoch, reverse=True)
        lines = []
        seen = set()
        for i in items:
            if i["titular"] in seen:
                continue
            seen.add(i["titular"])
            when = _fmt_when(i.get("fecha"))
            src = i.get("fuente") or ""
            head = " · ".join(x for x in (when, src) if x)
            lines.append(f"- {i['titular']} ({head})\n  {i.get('enlace', '')}".rstrip())
            if len(lines) >= limit:
                break
        if not lines:
            msg = (
                "No he encontrado titulares"
                + (f" sobre «{buscar}»" if buscar else "")
                + "."
            )
        else:
            msg = "\n".join(lines)
        if failed:
            msg += "\n(No he podido leer: " + ", ".join(failed) + ")"
        return msg

    @tool(
        "news_sources",
        "Lista las fuentes de noticias disponibles (las de serie y las que ha añadido el usuario).",
        integration="news",
    )
    def news_sources() -> str:
        lines = [f"- {key}: {name}" for key, (name, _) in DEFAULT_FEEDS.items()]
        lines.append("- hackernews: Hacker News (tecnología, en inglés)")
        if own:
            lines.append("Fuentes propias (usa «mías»):")
            lines.extend(f"- {_host(u)}: {u}" for u in own)
        else:
            lines.append(
                "El usuario no ha añadido fuentes propias (puede hacerlo en Conectores → Noticias)."
            )
        return "\n".join(lines)

    return [news_headlines, news_sources]


register(
    Integration(
        id="news",
        name="Noticias",
        category="personal",
        description="Titulares de la prensa española, Hacker News y tus propias fuentes. Siempre activo.",
        abilities=[
            "¿Qué noticias hay hoy?",
            "Titulares de Expansión sobre la bolsa",
            "¿Qué se cuenta de la vivienda en las noticias?",
        ],
        steps=[
            "No hace falta configurar nada: ya está activo con El País, RTVE, El Mundo, Expansión y más.",
            "Si quieres, añade tus propias fuentes: busca en tu web o blog favorito el enlace <b>RSS</b> (a veces aparece como «RSS», «Feed» o el icono 📶 naranja) y cópialo.",
            "Pega aquí los enlaces, uno por línea, y guarda. Luego pide «noticias de mis fuentes».",
        ],
        fields=[
            Field(
                "feeds",
                "Tus fuentes RSS (opcional)",
                kind="textarea",
                placeholder="https://www.ejemplo.com/rss\nhttps://blog.ejemplo.es/feed",
                help="Un enlace por línea. Valen RSS y Atom.",
                optional=True,
            )
        ],
        check=_check,
        build_tools=_tools,
        prompt="news_headlines da titulares (prensa española, Hacker News y fuentes del usuario), filtrables por fuente y palabra; news_sources lista las fuentes.",
        always_on=True,
        icon="📰",
        color="#e53935",
        link="https://news.ycombinator.com",
    )
)
