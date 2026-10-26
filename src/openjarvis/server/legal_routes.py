"""Legal pages (aviso legal, términos, privacidad, cookies) and acceptance.

The texts live in ``jarvis_ui/legal/``; company data comes from environment
variables (Railway shared variables), so it is filled in once for every
instance:

``LEGAL_COMPANY``, ``LEGAL_NIF``, ``LEGAL_ADDRESS``, ``LEGAL_REGISTRY``,
``LEGAL_EMAIL``, ``LEGAL_DPO``, ``LEGAL_JURISDICTION``, ``LEGAL_HOSTING``,
``LEGAL_BRAND`` (default "AI Fast Track").

While any required value is missing the pages show a "draft" banner and the
gaps are highlighted.  Users must accept the current ``LEGAL_VERSION`` in the
setup wizard; the acceptance is stored in ``<home>/legal_acceptance.json``.
"""

from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path
from typing import Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from openjarvis.core.paths import get_config_dir
from openjarvis.server.web_session import client_ip

router = APIRouter(tags=["legal"])

# Bump when the texts change materially: users are asked to accept again.
LEGAL_VERSION = "2026-10-26.4"
LEGAL_UPDATED = "26 de octubre de 2026"

_DIR = Path(__file__).parent / "jarvis_ui" / "legal"
DOCS = {
    "aviso-legal": ("Aviso legal", "NAV_AVISO"),
    "terminos": ("Términos de uso", "NAV_TERMINOS"),
    "privacidad": ("Política de privacidad", "NAV_PRIVACIDAD"),
    "cookies": ("Política de cookies", "NAV_COOKIES"),
}
# placeholder -> (env var, text shown while missing, required)
_FIELDS = {
    "COMPANY": ("LEGAL_COMPANY", "[Razón social]", True),
    "NIF": ("LEGAL_NIF", "[NIF]", True),
    "ADDRESS": ("LEGAL_ADDRESS", "[Domicilio social]", True),
    "REGISTRY": ("LEGAL_REGISTRY", "[Datos del Registro Mercantil]", True),
    "EMAIL": ("LEGAL_EMAIL", "[email de contacto]", True),
    "DPO": ("LEGAL_DPO", "No designado", False),
    "JURISDICTION": ("LEGAL_JURISDICTION", "[ciudad]", True),
    "HOSTING": ("LEGAL_HOSTING", "Estados Unidos", False),
    "BRAND": ("LEGAL_BRAND", "AI Fast Track", False),
}


def legal_values() -> Dict[str, str]:
    """Placeholder -> HTML-safe value (missing required ones highlighted)."""
    values: Dict[str, str] = {}
    for name, (env, fallback, required) in _FIELDS.items():
        value = os.environ.get(env, "").strip()
        if value:
            if name == "EMAIL":
                safe = html.escape(value)
                values[name] = f'<a href="mailto:{safe}">{safe}</a>'
            else:
                values[name] = html.escape(value)
        elif required:
            values[name] = f'<span class="ph">{html.escape(fallback)}</span>'
        else:
            values[name] = html.escape(fallback)
    return values


def legal_complete() -> bool:
    return all(
        os.environ.get(env, "").strip()
        for env, _, required in _FIELDS.values()
        if required
    )


def render_legal(doc: str) -> str:
    title, nav = DOCS[doc]
    values = legal_values()
    body = (_DIR / f"{doc}.html").read_text(encoding="utf-8")
    page = (_DIR / "_layout.html").read_text(encoding="utf-8")
    banner = (
        ""
        if legal_complete()
        else '<p class="draft">Borrador: faltan datos de la empresa (resaltados) '
        "y el texto está pendiente de revisión legal.</p>"
    )
    replacements = {
        "BODY": body,
        "TITLE": html.escape(title),
        "VERSION": LEGAL_VERSION,
        "UPDATED": LEGAL_UPDATED,
        "DRAFT_BANNER": banner,
        **{n: ("on" if n == nav else "") for _, n in DOCS.values()},
    }
    # BODY first, so placeholders inside the document get filled too.
    for key in ("BODY", *[k for k in replacements if k != "BODY"]):
        page = page.replace("{{" + key + "}}", replacements[key])
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)
    return page


@router.get("/legal/{doc}", response_class=HTMLResponse, include_in_schema=False)
async def legal_page(doc: str) -> HTMLResponse:
    if doc not in DOCS:
        raise HTTPException(status_code=404)
    return HTMLResponse(render_legal(doc), headers={"Cache-Control": "no-cache"})


@router.get("/privacidad", include_in_schema=False)
async def legacy_privacy() -> RedirectResponse:
    return RedirectResponse("/legal/privacidad", status_code=301)


# -- acceptance --------------------------------------------------------------


def _acceptance_path() -> Path:
    return get_config_dir() / "legal_acceptance.json"


def acceptance() -> Dict[str, object]:
    try:
        data = json.loads(_acceptance_path().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {
        "version": LEGAL_VERSION,
        "accepted": data.get("version") == LEGAL_VERSION,
        "accepted_at": data.get("accepted_at", ""),
        "accepted_version": data.get("version", ""),
    }


class AcceptRequest(BaseModel):
    version: str
    terms: bool
    whatsapp_risk: bool


@router.get("/api/legal")
async def legal_status() -> Dict[str, object]:
    return acceptance()


@router.post("/api/legal/accept")
async def legal_accept(req: AcceptRequest, request: Request) -> Dict[str, object]:
    if req.version != LEGAL_VERSION:
        raise HTTPException(409, "Los textos legales han cambiado; recarga la página")
    if not (req.terms and req.whatsapp_risk):
        raise HTTPException(400, "Debes marcar todas las casillas para continuar")
    path = _acceptance_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "version": LEGAL_VERSION,
        "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ip": client_ip(request),
        "user_agent": request.headers.get("user-agent", "")[:300],
        "items": ["terminos", "privacidad", "riesgo_whatsapp"],
    }
    # Keep the history of acceptances (evidence), newest last.
    try:
        history = json.loads(path.read_text(encoding="utf-8")).get("history", [])
    except Exception:
        history = []
    path.write_text(
        json.dumps({**record, "history": [*history, record][-20:]}, indent=1),
        encoding="utf-8",
    )
    return acceptance()


__all__ = ["DOCS", "LEGAL_VERSION", "acceptance", "legal_complete", "render_legal"]
