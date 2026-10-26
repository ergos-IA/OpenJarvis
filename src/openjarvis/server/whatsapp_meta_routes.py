"""WhatsApp Business (Meta Cloud API): setup API and the public webhook.

* ``POST /api/whatsapp/meta/config``  save + validate the Meta credentials
* ``POST /api/whatsapp/meta/test``    send a test message to the owner
* ``GET/POST /meta/whatsapp/webhook`` Meta's webhook (verify challenge and
  HMAC-signed message deliveries); public, outside ``/api``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from openjarvis.channels.whatsapp_cloud import WhatsAppCloudChannel, find_cloud_channel

router = APIRouter(tags=["whatsapp-meta"])


def _channel(request: Request) -> Optional[WhatsAppCloudChannel]:
    return find_cloud_channel(getattr(request.app.state, "channel_bridge", None))


def _require(request: Request) -> WhatsAppCloudChannel:
    channel = _channel(request)
    if channel is None:
        raise HTTPException(404, "Esta instancia no usa WhatsApp Business")
    return channel


class MetaConfig(BaseModel):
    phone_number_id: str
    access_token: str
    owner_number: str
    app_secret: str = ""
    template_name: str = ""


@router.post("/api/whatsapp/meta/config")
async def meta_config(req: MetaConfig, request: Request) -> dict:
    channel = _require(request)
    try:
        info = await asyncio.to_thread(
            channel.configure,
            phone_number_id=req.phone_number_id,
            access_token=req.access_token,
            owner_number=req.owner_number,
            app_secret=req.app_secret,
            template_name=req.template_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"enabled": True, **info}


@router.post("/api/whatsapp/meta/test")
async def meta_test(request: Request) -> dict:
    channel = _require(request)
    own = channel.self_jid()
    if not own:
        raise HTTPException(400, "Configura primero WhatsApp Business")
    result = await asyncio.to_thread(
        channel.send_and_wait,
        own,
        "✅ Jarvis conectado a WhatsApp Business. Escríbeme aquí cuando quieras.",
    )
    if not result["ok"]:
        raise HTTPException(
            502,
            "WhatsApp no ha entregado el mensaje: "
            + (result["error"] or "error desconocido")
            + ". Si nunca has escrito a este número, mándale primero un «hola».",
        )
    return {"ok": True}


@router.get("/meta/whatsapp/webhook", include_in_schema=False)
async def meta_verify(request: Request) -> PlainTextResponse:
    channel = _channel(request)
    params = request.query_params
    token = params.get("hub.verify_token", "")
    if (
        channel is not None
        and params.get("hub.mode") == "subscribe"
        and channel.verify_token
        and hmac.compare_digest(token, channel.verify_token)
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


@router.post("/meta/whatsapp/webhook", include_in_schema=False)
async def meta_webhook(request: Request) -> dict:
    channel = _channel(request)
    body = await request.body()
    # Fail closed: without the app secret we cannot prove the sender is Meta.
    if channel is None or not channel.verify_signature(
        body, request.headers.get("x-hub-signature-256", "")
    ):
        raise HTTPException(403, "Invalid signature")
    try:
        payload: Any = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    await asyncio.to_thread(channel.handle_webhook, payload)
    return {"ok": True}


__all__ = ["router"]
