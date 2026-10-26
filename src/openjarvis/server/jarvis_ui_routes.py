"""Jarvis home page: animated landing plus the step-by-step setup wizard.

``GET /`` (and ``/inicio``) serve ``jarvis_ui/index.html``: a public,
self-contained page (no data in it) that links WhatsApp and email through the
``/api/whatsapp/*`` and ``/api/mail/*`` endpoints with the instance API key,
typed once or passed as ``/#key=...`` (fragments never reach the server).
``?paso=whatsapp|correo|estado`` opens a wizard step directly; the old
``/whatsapp`` and ``/correo`` pages redirect to those (browsers keep the
``#key=`` fragment across the redirect).

Brand assets (AI Fast Track logo, Space Grotesk and Inter fonts, both OFL)
are served from ``/jarvis-ui/`` because the CSP only allows same-origin
resources.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

router = APIRouter(tags=["jarvis-ui"])

_UI_DIR = Path(__file__).parent / "jarvis_ui"
_ASSETS = {
    "aifasttrack-logo.svg": "image/svg+xml",
    "aifasttrack-mark.svg": "image/svg+xml",
    "space-grotesk.woff2": "font/woff2",
    "inter.woff2": "font/woff2",
}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/inicio", response_class=HTMLResponse, include_in_schema=False)
async def jarvis_home() -> HTMLResponse:
    html = (_UI_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/whatsapp", include_in_schema=False)
async def legacy_whatsapp_page() -> RedirectResponse:
    return RedirectResponse("/?paso=whatsapp", status_code=307)


@router.get("/correo", include_in_schema=False)
async def legacy_mail_page() -> RedirectResponse:
    return RedirectResponse("/?paso=correo", status_code=307)


@router.get("/jarvis-ui/{name}", include_in_schema=False)
async def jarvis_asset(name: str) -> FileResponse:
    media_type = _ASSETS.get(name)
    if media_type is None:
        raise HTTPException(status_code=404)
    return FileResponse(
        _UI_DIR / name,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
