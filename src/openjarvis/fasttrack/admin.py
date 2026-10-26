"""AI Fast Track admin console: create and manage one Jarvis per user.

Every user gets a Railway service named ``jarvis-<slug>`` in this project,
with its own volume, domain and access key, built from the GitHub repo.
The console itself is a separate service of the same image
(``JARVIS_ROLE=admin``).

Environment:

* ``ADMIN_PASSWORD``     console password (required)
* ``RAILWAY_API_TOKEN``  workspace token able to manage this project
* ``JARVIS_REPO`` / ``JARVIS_BRANCH``  source of user instances
  (default ``ergos-IA/OpenJarvis`` / ``main``)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from openjarvis.fasttrack.models import (
    DEFAULT_MODEL,
    MODELS,
    PROVIDERS,
    describe,
    engine_for,
    provider_for,
)
from openjarvis.fasttrack.railway import RailwayClient, RailwayError
from openjarvis.server.web_session import client_ip

USER_PREFIX = "jarvis-"
_COOKIE = "ft_session"
_SESSION_S = 12 * 3600
_UI_DIR = Path(__file__).parent / "admin_ui"
_ASSET_DIR = Path(__file__).parent.parent / "server" / "jarvis_ui"
_ASSETS = {
    "aifasttrack-logo.svg": "image/svg+xml",
    "aifasttrack-mark.svg": "image/svg+xml",
    "space-grotesk.woff2": "font/woff2",
    "inter.woff2": "font/woff2",
    "jetbrains-mono.woff2": "font/woff2",
}
_CREDENTIALS = [p["credential"] for p in PROVIDERS.values()]
_LEGAL_VARS = [
    "LEGAL_COMPANY",
    "LEGAL_NIF",
    "LEGAL_ADDRESS",
    "LEGAL_REGISTRY",
    "LEGAL_EMAIL",
    "LEGAL_DPO",
    "LEGAL_JURISDICTION",
    "LEGAL_HOSTING",
]


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:30].strip("-") or "usuario"


def new_api_key() -> str:
    return "oj_sk_" + secrets.token_urlsafe(32)


# No 0/O, 1/I/L: easy to read aloud and type.  12 chars ~ 60 bits.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_access_code() -> str:
    """Short personal code for the public site (jarvis.aifasttrack.tech)."""
    chars = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return "-".join(chars[i : i + 4] for i in range(0, 12, 4))


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


# ---------------------------------------------------------------------------
# Sessions (stateless signed cookie) and login throttling
# ---------------------------------------------------------------------------


class Auth:
    def __init__(self, password: str, secret: str) -> None:
        self.password = password
        self._key = hashlib.sha256(f"{secret}:{password}".encode()).digest()
        self._failures: Dict[str, List[float]] = {}

    def check_password(self, candidate: str, client: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < 600]
        if len(recent) >= 5:
            raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
        ok = bool(self.password) and hmac.compare_digest(
            candidate.encode(), self.password.encode()
        )
        if not ok:
            self._failures[client] = recent + [now]
        return ok

    def issue(self) -> str:
        expires = str(int(time.time()) + _SESSION_S)
        sig = hmac.new(self._key, expires.encode(), hashlib.sha256).digest()
        return expires + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def valid(self, token: str) -> bool:
        expires, _, sig = (token or "").partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        good = hmac.new(self._key, expires.encode(), hashlib.sha256).digest()
        good_b64 = base64.urlsafe_b64encode(good).decode().rstrip("=")
        return hmac.compare_digest(sig, good_b64)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    password: str


class NewUser(BaseModel):
    name: str
    email: str = ""
    model: str = DEFAULT_MODEL
    provider: str = ""  # only needed for model ids outside the catalog
    assistant_name: str = "Jarvis"


class ModelChange(BaseModel):
    model: str
    provider: str = ""


class DeleteRequest(BaseModel):
    confirm: str


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_admin_app(
    railway: Optional[RailwayClient] = None,
    *,
    password: Optional[str] = None,
    http: Optional[httpx.AsyncClient] = None,
) -> FastAPI:
    rw = railway or RailwayClient()
    auth = Auth(
        password if password is not None else os.environ.get("ADMIN_PASSWORD", ""),
        os.environ.get("RAILWAY_SERVICE_ID", "") or "ft-admin",
    )
    own_service = os.environ.get("RAILWAY_SERVICE_ID", "")
    repo = os.environ.get("JARVIS_REPO", "ergos-IA/OpenJarvis")
    branch = os.environ.get("JARVIS_BRANCH", "main")
    client = http or httpx.AsyncClient(timeout=6)
    app = FastAPI(title="AI Fast Track · Consola", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' 'unsafe-inline'; img-src 'self' data:"
        )
        return response

    def require(request: Request) -> None:
        if not auth.valid(request.cookies.get(_COOKIE, "")):
            raise HTTPException(401, "Inicia sesión")
        # JSON API only: a cross-site form can't set this header.
        if request.method != "GET" and request.headers.get("x-requested-with") != (
            "fasttrack"
        ):
            raise HTTPException(403, "Petición no válida")

    def guard(service_id: str) -> Dict[str, Any]:
        for svc in rw.project_overview()["services"]:
            if svc["id"] == service_id and _is_user(svc):
                return svc
        raise HTTPException(404, "Usuario no encontrado")

    def _is_user(svc: Dict[str, Any]) -> bool:
        return svc["name"].startswith(USER_PREFIX) and svc["id"] != own_service

    def railway_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RailwayError as exc:
            raise HTTPException(502, f"Railway: {exc}") from exc

    # -- pages and assets -------------------------------------------------

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def page() -> HTMLResponse:
        html = (_UI_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/jarvis-ui/{name}")
    async def asset(name: str) -> FileResponse:
        if name not in _ASSETS:
            raise HTTPException(404)
        return FileResponse(_ASSET_DIR / name, media_type=_ASSETS[name])

    # -- session ----------------------------------------------------------

    @app.post("/api/login")
    async def login(req: LoginRequest, request: Request, response: Response):
        ip = client_ip(request)
        if not auth.password:
            raise HTTPException(503, "Falta ADMIN_PASSWORD en la consola")
        if not auth.check_password(req.password, ip):
            raise HTTPException(401, "Contraseña incorrecta")
        response.set_cookie(
            _COOKIE,
            auth.issue(),
            max_age=_SESSION_S,
            httponly=True,
            secure=request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https",
            samesite="strict",
        )
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(_COOKIE)
        return {"ok": True}

    # -- overview ---------------------------------------------------------

    async def _live(domain: str, key: str) -> Dict[str, Any]:
        if not domain or not key:
            return {}
        headers = {"Authorization": f"Bearer {key}"}
        base = f"https://{domain}"
        out: Dict[str, Any] = {"online": False}
        try:
            wa, mail, info = await asyncio.gather(
                client.get(base + "/api/whatsapp/status", headers=headers),
                client.get(base + "/api/mail/status", headers=headers),
                client.get(base + "/api/jarvis/info", headers=headers),
                return_exceptions=True,
            )
            if isinstance(info, httpx.Response) and info.status_code == 200:
                out["legal_accepted"] = bool(info.json().get("legal_accepted"))
            if isinstance(wa, httpx.Response) and wa.status_code == 200:
                data = wa.json()
                out.update(
                    online=True,
                    whatsapp=bool(data.get("connected")),
                    phone=data.get("me", ""),
                )
            if isinstance(mail, httpx.Response) and mail.status_code == 200:
                data = mail.json()
                out["mail"] = [
                    k
                    for k in ("outlook", "gmail")
                    if (data.get(k) or {}).get("state") == "connected"
                ]
        except Exception:
            pass
        return out

    async def _user(svc: Dict[str, Any]) -> Dict[str, Any]:
        try:
            variables, domain = await asyncio.gather(
                asyncio.to_thread(rw.variables, svc["id"]),
                asyncio.to_thread(rw.domain, svc["id"]),
            )
        except RailwayError:
            variables, domain = {}, ""
        key = variables.get("OPENJARVIS_API_KEY", "")
        access_code = variables.get("JARVIS_ACCESS_CODE", "")
        model = variables.get("JARVIS_MODEL", "")
        live = await _live(domain, key)
        return {
            "id": svc["id"],
            "service": svc["name"],
            "name": variables.get("JARVIS_USER_NAME")
            or svc["name"][len(USER_PREFIX) :],
            "email": variables.get("JARVIS_USER_EMAIL", ""),
            "assistant": variables.get("JARVIS_ASSISTANT_NAME", "Jarvis"),
            "model": describe(model, variables.get("JARVIS_ENGINE", "")),
            "created_at": svc["created_at"],
            "deploy_status": svc["deploy_status"] or "SIN DESPLEGAR",
            "domain": domain,
            "link": f"https://{domain}/#key={key}" if domain and key else "",
            "access_code": access_code,
            **live,
        }

    @app.get("/api/overview")
    async def overview(request: Request) -> Dict[str, Any]:
        require(request)
        if not rw.configured:
            return {"railway": False, "users": [], "models": MODELS}
        overview = await asyncio.to_thread(railway_call, rw.project_overview)
        services = overview["services"]
        shared = await asyncio.to_thread(railway_call, rw.variables)
        users = await asyncio.gather(*[_user(s) for s in services if _is_user(s)])
        users.sort(key=lambda u: u["created_at"], reverse=True)
        return {
            "railway": True,
            "repo": f"{repo}@{branch}",
            "credentials": {c: bool(shared.get(c)) for c in _CREDENTIALS},
            "providers": PROVIDERS,
            "models": MODELS,
            "users": users,
        }

    # -- user lifecycle ---------------------------------------------------

    def _model_vars(model: str, provider: str = "") -> Dict[str, str]:
        model = model.strip()
        if not model:
            raise HTTPException(400, "Elige un modelo")
        if provider and provider not in PROVIDERS:
            raise HTTPException(400, "Proveedor desconocido")
        provider = provider or provider_for(model)
        return {"JARVIS_MODEL": model, "JARVIS_ENGINE": engine_for(provider)}

    @app.post("/api/users")
    async def create_user(req: NewUser, request: Request) -> Dict[str, Any]:
        require(request)
        name = " ".join(req.name.split())
        if not name:
            raise HTTPException(400, "Escribe el nombre del usuario")
        overview = await asyncio.to_thread(railway_call, rw.project_overview)
        taken = {s["name"] for s in overview["services"]}
        base = USER_PREFIX + slugify(name)
        service_name, n = base, 2
        while service_name in taken:
            service_name, n = f"{base}-{n}", n + 1

        key = new_api_key()
        access_code = new_access_code()
        variables = {
            "PORT": "8000",
            "RAILWAY_DOCKERFILE_PATH": "deploy/railway/Dockerfile",
            "OPENJARVIS_API_KEY": key,
            "JARVIS_ACCESS_CODE": access_code,
            "JARVIS_ASSISTANT_NAME": req.assistant_name.strip() or "Jarvis",
            "JARVIS_USER_NAME": name,
            "JARVIS_USER_EMAIL": req.email.strip(),
            "WHATSAPP_MODE": "self",
            **_model_vars(req.model, req.provider),
        }
        # Provider credentials and the company's legal data come from the
        # project's shared variables, filled in once for every user.
        for name in _CREDENTIALS + _LEGAL_VARS:
            variables[name] = "${{shared.%s}}" % name

        def provision() -> Dict[str, str]:
            sid = rw.create_service(service_name)
            rw.set_variables(sid, variables)
            rw.create_volume(sid, "/data")
            domain = rw.create_domain(sid, 8000)
            rw.set_healthcheck(sid, "/health")
            build_error = ""
            try:
                rw.connect_repo(sid, repo, branch)
            except RailwayError as exc:
                build_error = str(exc)
            return {"id": sid, "domain": domain, "build_error": build_error}

        result = await asyncio.to_thread(railway_call, provision)
        return {
            "id": result["id"],
            "service": service_name,
            "link": f"https://{result['domain']}/#key={key}",
            "access_code": access_code,
            "build_error": result["build_error"],
        }

    @app.patch("/api/users/{service_id}")
    async def change_model(
        service_id: str, req: ModelChange, request: Request
    ) -> Dict[str, Any]:
        require(request)
        await asyncio.to_thread(guard, service_id)
        variables = _model_vars(req.model, req.provider)
        await asyncio.to_thread(
            railway_call, rw.set_variables, service_id, variables, deploy=True
        )
        return {
            "ok": True,
            "model": describe(variables["JARVIS_MODEL"], variables["JARVIS_ENGINE"]),
        }

    @app.post("/api/users/{service_id}/rotate-key")
    async def rotate_key(service_id: str, request: Request) -> Dict[str, Any]:
        require(request)
        await asyncio.to_thread(guard, service_id)
        key, access_code = new_api_key(), new_access_code()
        await asyncio.to_thread(
            railway_call,
            rw.set_variables,
            service_id,
            {"OPENJARVIS_API_KEY": key, "JARVIS_ACCESS_CODE": access_code},
            deploy=True,
        )
        domain = await asyncio.to_thread(rw.domain, service_id)
        return {
            "ok": True,
            "link": f"https://{domain}/#key={key}",
            "access_code": access_code,
        }

    @app.post("/api/users/{service_id}/redeploy")
    async def redeploy(service_id: str, request: Request) -> Dict[str, Any]:
        require(request)
        svc = await asyncio.to_thread(guard, service_id)
        if svc["deploy_status"]:
            await asyncio.to_thread(railway_call, rw.redeploy, service_id)
        else:
            # Never built (e.g. GitHub wasn't connected when it was created).
            await asyncio.to_thread(
                railway_call, rw.connect_repo, service_id, repo, branch
            )
        return {"ok": True}

    @app.delete("/api/users/{service_id}")
    async def delete_user(
        service_id: str, req: DeleteRequest, request: Request
    ) -> Dict[str, Any]:
        require(request)
        svc = await asyncio.to_thread(guard, service_id)
        if req.confirm.strip().lower() != svc["name"][len(USER_PREFIX) :]:
            raise HTTPException(400, "El texto de confirmación no coincide")
        await asyncio.to_thread(
            railway_call, rw.delete_service, service_id, svc["volume_id"]
        )
        return {"ok": True}

    return app


__all__ = [
    "Auth",
    "create_admin_app",
    "new_access_code",
    "new_api_key",
    "normalize_code",
    "slugify",
]
