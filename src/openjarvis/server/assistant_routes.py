"""Assistant behaviour settings for the user's portal ("Ajustes del asistente").

Live settings (confirmations, style, name, instructions) apply to the next
message.  Engine settings (creativity, reply length, steps, memory) are copied
into ``config.toml`` at start-up, so changing them restarts the service.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["assistant"])


class AssistantSettings(BaseModel):
    confirm: Optional[str] = None
    style: Optional[str] = None
    address: Optional[str] = None
    assistant_name: Optional[str] = None
    instructions: Optional[str] = None
    engine: Optional[Dict[str, Any]] = None


@router.get("/api/assistant/settings")
async def get_assistant_settings() -> Dict[str, Any]:
    from openjarvis.personal.assistant_prefs import get_prefs

    return get_prefs().settings()


@router.post("/api/assistant/settings")
async def set_assistant_settings(req: AssistantSettings) -> Dict[str, Any]:
    from openjarvis.personal.assistant_prefs import get_prefs
    from openjarvis.server.ai_routes import restart_server

    prefs = get_prefs()
    try:
        engine_changed = prefs.update(**req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    restarting = restart_server() if engine_changed else False
    return {**prefs.settings(), "restarting": restarting}


__all__ = ["router"]
