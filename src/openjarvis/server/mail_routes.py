"""Email account linking API, used by the setup wizard at ``/``.

Gmail links with address + app password, Outlook / Microsoft 365 with a
device-code sign-in.  See ``jarvis_ui_routes`` for the page.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openjarvis.mail.service import get_mail_service

router = APIRouter(tags=["mail"])


class GmailLink(BaseModel):
    email: str
    app_password: str


@router.on_event("startup")
async def _start_mail_poller() -> None:
    get_mail_service().start()


@router.get("/api/mail/status")
async def mail_status() -> dict:
    return get_mail_service().status()


@router.post("/api/mail/gmail")
async def mail_link_gmail(req: GmailLink) -> dict:
    svc = get_mail_service()
    try:
        await asyncio.to_thread(svc.connect_gmail, req.email, req.app_password)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Gmail rechazó el acceso: {str(exc)[:200]}",
        ) from exc
    return svc.status()


@router.delete("/api/mail/gmail")
async def mail_unlink_gmail() -> dict:
    svc = get_mail_service()
    svc.disconnect_gmail()
    return svc.status()


@router.post("/api/mail/outlook/start")
async def mail_outlook_start() -> dict:
    svc = get_mail_service()
    if svc.ms_auth is None:
        raise HTTPException(
            status_code=404, detail="Outlook no está configurado (falta MS_CLIENT_ID)"
        )
    try:
        await asyncio.to_thread(svc.ms_auth.start_device_flow)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Microsoft: {str(exc)[:200]}"
        ) from exc
    return svc.status()


@router.delete("/api/mail/outlook")
async def mail_unlink_outlook() -> dict:
    svc = get_mail_service()
    svc.disconnect_outlook()
    return svc.status()
