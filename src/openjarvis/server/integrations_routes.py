"""Connectors to external tools, for the user's setup page.

* ``GET /api/integrations``                 catalogue + what is connected
* ``POST /api/integrations/{id}``           connect with pasted values
* ``DELETE /api/integrations/{id}``         disconnect (forget credentials)
* ``POST /api/integrations/{id}/oauth``     URL to start an OAuth connection
* ``GET /oauth/done``                       end of OAuth (bounced here by the
  public site's ``/oauth/callback``); not under ``/api`` because it is a
  plain browser navigation -- the single-use nonce in ``state`` protects it.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from openjarvis.integrations.base import IntegrationError
from openjarvis.integrations.manager import get_manager

router = APIRouter(tags=["integrations"])


@router.get("/api/integrations")
async def list_integrations() -> Dict[str, Any]:
    items = await asyncio.to_thread(get_manager().catalog)
    return {"integrations": items}


@router.post("/api/integrations/{integration_id}")
async def connect(integration_id: str, request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "Datos no válidos")
    try:
        return await asyncio.to_thread(get_manager().connect, integration_id, body)
    except IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/integrations/{integration_id}")
async def disconnect(integration_id: str) -> Dict[str, Any]:
    try:
        await asyncio.to_thread(get_manager().disconnect, integration_id)
    except IntegrationError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@router.post("/api/integrations/{integration_id}/oauth")
async def oauth_start(integration_id: str, request: Request) -> Dict[str, str]:
    host = request.headers.get("x-forwarded-host") or request.url.hostname or ""
    try:
        url = get_manager().begin_oauth(integration_id, host.split(",")[0].strip())
    except IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"url": url}


@router.get("/oauth/done")
async def oauth_done(code: str = "", state: str = "", error: str = "") -> Any:
    if error or not code:
        return _page(
            "No se ha completado la conexión", error or "Has cancelado el acceso."
        )
    try:
        integration_id = await asyncio.to_thread(
            get_manager().finish_oauth, code, state
        )
    except IntegrationError as exc:
        return _page("No se ha podido conectar", str(exc))
    return RedirectResponse(f"/#conectores={integration_id}", status_code=303)


def _page(title: str, detail: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport "
        "content='width=device-width,initial-scale=1'><title>Jarvis</title>"
        "<body style='background:#040506;color:#e8ecef;font-family:system-ui;"
        "display:grid;place-items:center;min-height:100vh;margin:0;padding:16px'>"
        "<div style='max-width:420px'>"
        f"<h1 style='font-weight:500'>{html.escape(title)}</h1>"
        f"<p style='color:#aab4bd'>{html.escape(detail)}</p>"
        "<p><a href='/#conectores' style='color:#00d9ff'>Volver a Jarvis</a></p></div>",
        status_code=400,
    )


__all__ = ["router"]
