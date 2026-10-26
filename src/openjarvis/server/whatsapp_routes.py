"""WhatsApp (Baileys) linking API, used by the setup wizard at ``/``.

Linking is by QR code or, when the page is open on the phone itself, by an
8-character pairing code typed in WhatsApp.  See ``jarvis_ui_routes``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["whatsapp"])


@router.on_event("startup")
async def _start_reminders() -> None:
    # Reminders and scheduled tasks are delivered to the WhatsApp chat.
    from openjarvis.personal.reminders import get_reminder_service

    get_reminder_service().start()


def _baileys_channel(request: Request) -> Optional[Any]:
    """The instance's WhatsApp channel: Baileys (personal) or Cloud API."""
    bridge = getattr(request.app.state, "channel_bridge", None)
    if bridge is None:
        return None
    if getattr(bridge, "channel_id", "") in ("whatsapp_baileys", "whatsapp_cloud"):
        return bridge
    channels = getattr(bridge, "_channels", None) or {}
    return channels.get("whatsapp_baileys") or channels.get("whatsapp_cloud")


@router.get("/api/whatsapp/status")
async def whatsapp_status(request: Request) -> dict:
    channel = _baileys_channel(request)
    if channel is None:
        return {"enabled": False}
    return {"enabled": True, **channel.pairing_info()}


@router.post("/api/whatsapp/logout")
async def whatsapp_logout(request: Request) -> dict:
    channel = _baileys_channel(request)
    if channel is None:
        raise HTTPException(status_code=404, detail="WhatsApp is not enabled")
    if not channel.logout():
        raise HTTPException(status_code=503, detail="WhatsApp bridge not running")
    return {"status": "ok"}


class PairingCodeRequest(BaseModel):
    phone: str


@router.post("/api/whatsapp/pairing-code")
async def whatsapp_pairing_code(request: Request, req: PairingCodeRequest) -> dict:
    channel = _baileys_channel(request)
    if channel is None:
        raise HTTPException(status_code=404, detail="WhatsApp is not enabled")
    phone = "".join(ch for ch in req.phone if ch.isdigit())
    if len(phone) == 9:
        phone = "34" + phone  # Spanish mobile without prefix
    if len(phone) < 10:
        raise HTTPException(status_code=400, detail="Número no válido")
    if not channel.request_pairing_code(phone):
        raise HTTPException(status_code=503, detail="WhatsApp bridge not running")
    return {"status": "requested"}
