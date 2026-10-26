"""Bring-your-own AI account: the user connects their own API key.

``GET /api/ai/status``  which AI this Jarvis uses and who pays for it
``POST /api/ai/config``  validate a key with the provider, save it to
                          ``<home>/ai.json`` and restart the server to use it
``DELETE /api/ai/config`` forget it

The key is stored only in the instance's own volume (mode 0600) and is
never returned by the API.  ``deploy/railway/bootstrap_config.py`` applies
``ai.json`` over the environment at start-up; restarting means re-exec'ing
that launcher (``JARVIS_LAUNCHER``), which keeps the container -- and the
WhatsApp session -- alive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openjarvis.core.paths import get_config_dir
from openjarvis.fasttrack.credits import get_meter
from openjarvis.fasttrack.models import MODELS, PROVIDERS, describe

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ai"])

# provider -> (env var, validation request)
_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-\.]{20,300}$")
_RESTART_DELAY_S = 1.5


def _path() -> Path:
    return get_config_dir() / "ai.json"


def _saved() -> Dict[str, Any]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def ai_status() -> Dict[str, Any]:
    saved = _saved()
    model = os.environ.get("JARVIS_MODEL", "")
    engine = os.environ.get("JARVIS_ENGINE", "")
    info = describe(model, engine)
    provider = info["provider"]
    key_env = _KEY_ENV.get(provider, "")
    has_key = bool(key_env and os.environ.get(key_env)) or provider == "ollama"
    if saved.get("provider"):
        source = "own"
    elif has_key:
        source = "included"
    else:
        source = "none"
    pending = bool(saved.get("provider")) and (
        saved.get("env", {}).get("JARVIS_MODEL") != model
    )
    credits = get_meter().status()
    allowed = credits["models"]
    if source == "own":
        choices = [m for m in MODELS if m["provider"] == saved.get("provider")]
    elif allowed:
        choices = [m for m in MODELS if m["id"] in allowed]
    else:
        choices = []
    account_url = ""
    if os.environ.get("STRIPE_CUSTOMER_ID") and os.environ.get("JARVIS_ACCESS_CODE"):
        site = os.environ.get("PUBLIC_URL", "https://jarvis.aifasttrack.tech")
        account_url = f"{site.rstrip('/')}/cuenta#c={os.environ['JARVIS_ACCESS_CODE']}"
    return {
        **info,
        "credits": credits,
        "account_url": account_url,
        "model_choices": choices,
        "source": source,  # own: the user's account · included: ours · none
        "configured": source != "none",
        "restarting": pending,
        "saved_provider": saved.get("provider", ""),
        "key_hint": saved.get("key_hint", ""),
        "models": [m for m in MODELS if m["provider"] in _KEY_ENV],
        "providers": {k: PROVIDERS[k]["label"] for k in _KEY_ENV},
    }


def validate_key(
    provider: str, key: str, *, http: Optional[httpx.Client] = None
) -> None:
    """Raise ValueError unless the provider accepts *key*."""
    client = http or httpx.Client(timeout=15)
    try:
        if provider == "anthropic":
            resp = client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
        elif provider == "openai":
            resp = client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
        else:
            resp = client.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        raise ValueError(f"No se pudo contactar con el proveedor: {exc}") from exc
    if resp.status_code in (401, 403):
        raise ValueError(
            "El proveedor no acepta esa clave. Revísala y vuelve a pegarla."
        )
    if resp.status_code >= 400:
        raise ValueError(f"El proveedor respondió con un error ({resp.status_code}).")


def _write(data: Dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def restart_server() -> bool:
    """Re-exec the launcher shortly, so the response gets out first."""
    launcher = os.environ.get("JARVIS_LAUNCHER", "")
    if not launcher or not Path(launcher).exists():
        logger.warning("AI settings saved; restart the service to apply them")
        return False

    def _go() -> None:
        time.sleep(_RESTART_DELAY_S)
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, launcher, "--serve"])

    threading.Thread(target=_go, name="ai-restart", daemon=True).start()
    return True


class AIConfig(BaseModel):
    provider: str
    api_key: str
    model: str


@router.get("/api/ai/status")
async def get_ai_status() -> Dict[str, Any]:
    return ai_status()


@router.post("/api/ai/config")
async def set_ai_config(req: AIConfig) -> Dict[str, Any]:
    provider = req.provider.strip().lower()
    if provider not in _KEY_ENV:
        raise HTTPException(400, "Proveedor no admitido")
    key = req.api_key.strip()
    if not _KEY_RE.match(key):
        raise HTTPException(400, "Eso no parece una clave de API. Cópiala entera.")
    model = req.model.strip()
    if not any(m["id"] == model and m["provider"] == provider for m in MODELS):
        raise HTTPException(400, "Elige un modelo de ese proveedor")
    try:
        await asyncio.to_thread(validate_key, provider, key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _write(
        {
            "provider": provider,
            "key_hint": key[:7] + "…" + key[-4:],
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "env": {
                _KEY_ENV[provider]: key,
                "JARVIS_MODEL": model,
                "JARVIS_ENGINE": "cloud",
            },
        }
    )
    restarting = restart_server()
    return {**ai_status(), "restarting": restarting}


class ModelChoice(BaseModel):
    model: str


@router.post("/api/ai/model")
async def change_model(req: ModelChoice) -> Dict[str, Any]:
    """Switch model: free with your own AI account, once a day otherwise."""
    status = ai_status()
    model = req.model.strip()
    if model not in {m["id"] for m in status["model_choices"]}:
        raise HTTPException(400, "Ese modelo no está disponible en tu plan")
    meter = get_meter()
    if status["source"] != "own" and not meter.can_change_model():
        hours = max(1, round(status["credits"]["model_change_in_s"] / 3600))
        raise HTTPException(
            429, f"Puedes cambiar de modelo una vez al día (en ~{hours} h)."
        )
    saved = _saved()
    env = dict(saved.get("env") or {})
    env.update(JARVIS_MODEL=model, JARVIS_ENGINE="cloud")
    _write({**saved, "env": env})
    if status["source"] != "own":
        meter.mark_model_changed()
    restarting = restart_server()
    return {**ai_status(), "restarting": restarting}


class TopUp(BaseModel):
    credits: float


@router.post("/api/credits/topup")
async def credits_topup(req: TopUp, request: Request) -> Dict[str, Any]:
    """Add credits (console / billing only: needs the internal admin token)."""
    token = os.environ.get("FASTTRACK_ADMIN_TOKEN", "")
    import hmac

    if not token or not hmac.compare_digest(
        request.headers.get("x-fasttrack-admin", ""), token
    ):
        raise HTTPException(403, "Solo AI Fast Track puede añadir créditos")
    if not 0 < req.credits <= 100000:
        raise HTTPException(400, "Cantidad no válida")
    return get_meter().add_topup(req.credits)


@router.delete("/api/ai/config")
async def clear_ai_config() -> Dict[str, Any]:
    _path().unlink(missing_ok=True)
    restarting = restart_server()
    return {**ai_status(), "restarting": restarting}


__all__ = ["ai_status", "restart_server", "router", "validate_key"]
