"""Jarvis home page: animated landing plus the step-by-step setup wizard.

``GET /`` (and ``/inicio``) serve ``jarvis_ui/index.html`` only to a browser
with a session; anyone else gets ``acceso.html`` ("restricted").  The
personal link ``/#key=...`` (fragments never reach the server) is exchanged
once for an HttpOnly session cookie, see :mod:`openjarvis.server.web_session`.
The page then talks to ``/api/whatsapp/*`` and ``/api/mail/*`` with it.
``?paso=whatsapp|correo|estado`` opens a wizard step directly; the old
``/whatsapp`` and ``/correo`` pages redirect to those (browsers keep the
``#key=`` fragment across the redirect).

Brand assets (AI Fast Track logo, Space Grotesk and Inter fonts, both OFL)
are served from ``/jarvis-ui/`` because the CSP only allows same-origin
resources.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from openjarvis.server.web_session import (
    SESSION_COOKIE,
    SESSION_TTL_S,
    issue_session,
    session_valid,
)

router = APIRouter(tags=["jarvis-ui"])

_UI_DIR = Path(__file__).parent / "jarvis_ui"
_ASSETS = {
    "aifasttrack-logo.svg": "image/svg+xml",
    "aifasttrack-mark.svg": "image/svg+xml",
    "space-grotesk.woff2": "font/woff2",
    "inter.woff2": "font/woff2",
    "jetbrains-mono.woff2": "font/woff2",
}


def _api_key(request: Request) -> str:
    return getattr(request.app.state, "api_key", "") or os.environ.get(
        "OPENJARVIS_API_KEY", ""
    )


_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/inicio", response_class=HTMLResponse, include_in_schema=False)
async def jarvis_home(request: Request) -> HTMLResponse:
    """The app for a signed-in browser, otherwise the "restricted" page.

    The restricted page holds nothing but a short script that, when opened
    from a personal link (``#key=...``), exchanges the key for a session.
    """
    key = _api_key(request)
    signed_in = not key or session_valid(request.cookies.get(SESSION_COOKIE, ""), key)
    page = "index.html" if signed_in else "acceso.html"
    html = (_UI_DIR / page).read_text(encoding="utf-8")
    return HTMLResponse(html, headers=_PAGE_HEADERS)


@router.post("/api/session")
async def open_session(request: Request) -> Response:
    """Swap the personal key (``Authorization: Bearer``) for a session cookie.

    Reaching this handler means AuthMiddleware already validated the key.
    """
    response = Response(status_code=204)
    https = request.url.scheme == "https" or (
        request.headers.get("x-forwarded-proto", "") == "https"
    )
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(_api_key(request)),
        max_age=SESSION_TTL_S,
        httponly=True,
        secure=https,
        samesite="lax",
    )
    return response


@router.delete("/api/session")
async def close_session() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/whatsapp", include_in_schema=False)
async def legacy_whatsapp_page() -> RedirectResponse:
    return RedirectResponse("/?paso=whatsapp", status_code=307)


@router.get("/correo", include_in_schema=False)
async def legacy_mail_page() -> RedirectResponse:
    return RedirectResponse("/?paso=correo", status_code=307)


# The upstream SPA installed a PWA service worker (/sw.js) that keeps serving
# its cached UI. Browsers re-fetch /sw.js to update it: this replacement
# unregisters itself and reloads open tabs so they get the AI Fast Track page.
_KILL_SW = """
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.registration.unregister(); })
      .then(function () { return self.clients.matchAll({ type: "window" }); })
      .then(function (clients) {
        clients.forEach(function (c) { c.navigate(c.url); });
      })
  );
});
"""


@router.get("/sw.js", include_in_schema=False)
@router.get("/service-worker.js", include_in_schema=False)
async def retire_service_worker() -> Response:
    return Response(
        _KILL_SW,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/jarvis/info")
async def jarvis_info() -> dict:
    """Who this Jarvis is for and which model it runs (shown on the page)."""
    from openjarvis.fasttrack.models import describe
    from openjarvis.server.legal_routes import acceptance

    legal = acceptance()
    return {
        "assistant_name": os.environ.get("JARVIS_ASSISTANT_NAME", "Jarvis"),
        "user_name": os.environ.get("JARVIS_USER_NAME", ""),
        "legal_version": legal["version"],
        "legal_accepted": legal["accepted"],
        **describe(
            os.environ.get("JARVIS_MODEL", ""), os.environ.get("JARVIS_ENGINE", "")
        ),
    }


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
