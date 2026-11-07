"""Public product site (jarvis.aifasttrack.tech) with "access my Jarvis".

Serves the product page and the legal texts, and resolves a user's short
access code (``JARVIS_ACCESS_CODE``) -- or their full personal link/key -- to
their own instance, which then opens a session.  Codes are looked up through
the backend (Railway project or Docker containers, same as the console) and
cached briefly.

Two ways to reach an instance:

* Railway: every instance has its own domain; the site answers with
  ``https://<instance domain>/#key=...``.
* Docker on one VPS (single domain): the site sets the routing cookie
  ``jv=<slug>`` and sends the browser to ``/#key=...`` on this same domain;
  Traefik routes requests carrying the cookie to ``jarvis-<slug>``.  The
  paths below (``/salir``, ``/entrar``, ``/api/acceder``, ``/api/alta``,
  ``/api/cuenta/*``, ``/api/stripe/webhook``, ``/oauth/callback``,
  ``/api/contacto``, ``/gracias``, ``/cuenta``) always reach this site, with
  or without the cookie (see deploy/vps).  ``/salir`` forgets the cookie.

Brute force: each IP gets a few attempts per window, and a global cap stops
distributed guessing; codes carry ~60 bits, so online guessing is hopeless.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from openjarvis.fasttrack import billing
from openjarvis.fasttrack import contact as contact_mail
from openjarvis.fasttrack.backends import get_backend
from openjarvis.fasttrack.errors import BackendError
from openjarvis.fasttrack.plans import PLANS
from openjarvis.fasttrack.provisioning import USER_PREFIX, normalize_code
from openjarvis.server.legal_routes import DOCS, render_legal
from openjarvis.server.web_session import (
    SESSION_COOKIE,
    FailureLimiter,
    client_ip,
)

_UI_DIR = Path(__file__).parent / "landing_ui"
_ASSET_DIR = Path(__file__).parent.parent / "server" / "jarvis_ui"
_ASSETS = {
    "aifasttrack-logo.svg": "image/svg+xml",
    "aifasttrack-mark.svg": "image/svg+xml",
    "space-grotesk.woff2": "font/woff2",
    "inter.woff2": "font/woff2",
    "jetbrains-mono.woff2": "font/woff2",
}
logger = logging.getLogger(__name__)
_KEY_RE = re.compile(r"oj_sk_[A-Za-z0-9_\-]{20,}")
# Single-domain routing cookie read by Traefik (see docker_backend).
ROUTE_COOKIE = "jv"
ROUTE_COOKIE_MAX_AGE = 365 * 24 * 3600


class Directory:
    """Access code / key -> instance, refreshed from the backend."""

    def __init__(self, rw: Any, ttl_s: float = 300, min_gap_s: float = 30):
        self._rw = get_backend(rw)
        self._ttl = ttl_s
        self._min_gap = min_gap_s
        self._entries: List[Dict[str, Any]] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        entries = []
        for svc in self._rw.services():
            if not svc["name"].startswith(USER_PREFIX):
                continue
            variables = self._rw.variables(svc["id"])
            key = variables.get("OPENJARVIS_API_KEY", "")
            if not key:
                continue
            ends = self._rw.endpoints(svc["id"], key)
            entries.append(
                {
                    "id": svc["id"],
                    "service": svc["name"],
                    "aliases": svc.get("aliases") or [],
                    "code": normalize_code(variables.get("JARVIS_ACCESS_CODE", "")),
                    "key": key,
                    "domain": ends["host"],
                    "link": ends["link"],
                    "route": ends["route"],
                    "customer": variables.get("STRIPE_CUSTOMER_ID", ""),
                    "plan": variables.get("JARVIS_PLAN", "own"),
                    "name": variables.get("JARVIS_USER_NAME", ""),
                    "email": variables.get("JARVIS_USER_EMAIL", ""),
                }
            )
        self._entries = entries
        self._loaded_at = time.time()

    def _match(self, code: str, key: str) -> Optional[Dict[str, Any]]:
        found = None
        for entry in self._entries:  # constant work per entry
            code_ok = bool(code) and hmac.compare_digest(
                code.encode(), entry["code"].encode()
            )
            key_ok = bool(key) and hmac.compare_digest(
                key.encode(), entry["key"].encode()
            )
            if (code_ok or key_ok) and entry["link"]:
                found = entry
        return found

    def find(self, *, customer: str) -> Optional[Dict[str, Any]]:
        """The instance of a Stripe customer (fresh data: used by billing)."""
        if not customer:
            return None
        with self._lock:
            self._refresh()
            return next((e for e in self._entries if e["customer"] == customer), None)

    def has_domain(self, host: str) -> bool:
        """Is *host* one of our users' instances? (OAuth relay target check).

        On the single-domain setup every instance answers at the public host.
        """
        host = (host or "").lower()
        if not host:
            return False
        with self._lock:
            if time.time() - self._loaded_at > self._ttl:
                self._refresh()
            if any(e["domain"].lower() == host for e in self._entries):
                return True
            if time.time() - self._loaded_at > self._min_gap:
                self._refresh()
                return any(e["domain"].lower() == host for e in self._entries)
        return False

    def resolve(self, text: str) -> Optional[str]:
        entry = self.entry(text)
        if entry is None:
            return None
        return entry["link"]

    def entry(self, text: str) -> Optional[Dict[str, Any]]:
        text = (text or "").strip()
        key_match = _KEY_RE.search(text)
        key = key_match.group(0) if key_match else ""
        code = "" if key else normalize_code(text)
        if not key and len(code) != 12:
            return None
        with self._lock:
            age = time.time() - self._loaded_at
            if age > self._ttl:
                self._refresh()
            entry = self._match(code, key)
            if entry is None and age > self._min_gap:
                # A user created a minute ago: refresh, but not on every miss.
                self._refresh()
                entry = self._match(code, key)
        return entry


class AccessRequest(BaseModel):
    code: str


class SignupRequest(BaseModel):
    plan: str
    interval: str = "month"
    assisted: bool = False


def create_landing_app(
    railway: Any = None,
    *,
    per_ip: Optional[FailureLimiter] = None,
    global_limit: Optional[FailureLimiter] = None,
    mail_http=None,  # noqa: ANN001 - injected httpx.AsyncClient for tests
    stripe: Optional[billing.StripeClient] = None,
    fulfillment: Optional[billing.Fulfillment] = None,
) -> FastAPI:
    rw = get_backend(railway)
    directory = Directory(rw)
    stripe = stripe or billing.StripeClient()
    fulfillment = fulfillment or billing.Fulfillment(rw, directory.find)
    signup_ip = FailureLimiter(max_failures=10, window_s=3600, block_s=1800)
    per_ip = per_ip or FailureLimiter(max_failures=8, window_s=600, block_s=1800)
    # Many failures from everywhere at once = distributed guessing: pause all.
    global_limit = global_limit or FailureLimiter(
        max_failures=300, window_s=600, block_s=600
    )
    contact = os.environ.get("LANDING_CONTACT_URL", "#contacto")
    # Contact form: a few messages per IP per hour, and a global ceiling.
    contact_ip = FailureLimiter(max_failures=3, window_s=3600, block_s=3600)
    contact_all = FailureLimiter(max_failures=60, window_s=3600, block_s=1800)
    app = FastAPI(title="Jarvis · AI Fast Track", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    @app.get("/entrar", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        # /entrar is the personal link (/entrar#key=...): always this site
        # (even with another user's jv cookie); the page swaps the key for
        # the routing cookie and opens the user's Jarvis.
        html = (_UI_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("{{CONTACT_URL}}", contact)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/salir")
    async def switch_user() -> RedirectResponse:
        """Forget which Jarvis this browser opens and go back to the site."""
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            ROUTE_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
        )
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/gracias", response_class=HTMLResponse)
    async def thanks_page() -> HTMLResponse:
        html = (_UI_DIR / "gracias.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/cuenta", response_class=HTMLResponse)
    async def account_page() -> HTMLResponse:
        html = (_UI_DIR / "cuenta.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/legal/{doc}", response_class=HTMLResponse)
    async def legal(doc: str) -> HTMLResponse:
        if doc not in DOCS:
            raise HTTPException(404)
        return HTMLResponse(render_legal(doc))

    @app.get("/jarvis-ui/{name}")
    async def asset(name: str) -> FileResponse:
        if name not in _ASSETS:
            raise HTTPException(404)
        return FileResponse(
            _ASSET_DIR / name,
            media_type=_ASSETS[name],
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post("/api/acceder")
    async def access(
        req: AccessRequest, request: Request, response: Response
    ) -> Dict[str, str]:
        entry = await _entry_for_code(req.code, request)
        if not entry["route"]:
            return {"url": entry["link"]}  # instance with its own domain
        # Single domain: Traefik sends requests with jv=<slug> to the instance.
        response.set_cookie(
            ROUTE_COOKIE,
            entry["route"],
            max_age=ROUTE_COOKIE_MAX_AGE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"url": f"/#key={entry['key']}"}

    async def _entry_for_code(code: str, request: Request) -> Dict[str, str]:
        ip = client_ip(request)
        if per_ip.blocked(ip) or global_limit.blocked("*"):
            raise HTTPException(
                429, "Demasiados intentos. Por seguridad, espera un rato."
            )
        try:
            entry = await asyncio.to_thread(directory.entry, code[:300])
        except BackendError:
            raise HTTPException(503, "Servicio no disponible. Prueba en unos minutos.")
        if entry is None:
            per_ip.fail(ip)
            global_limit.fail("*")
            await asyncio.sleep(0.4)
            raise HTTPException(404, "Código no válido. Revísalo o pide uno nuevo.")
        per_ip.succeed(ip)
        return entry

    def _need_stripe() -> None:
        if not billing.configured():
            raise HTTPException(
                503,
                "El pago online no está disponible ahora. "
                "Escríbenos a hola@aifasttrack.tech.",
            )

    @app.post("/api/alta")
    async def signup(req: SignupRequest, request: Request) -> Dict[str, str]:
        _need_stripe()
        ip = client_ip(request)
        if signup_ip.blocked(ip):
            raise HTTPException(429, "Demasiados intentos. Espera un rato.")
        signup_ip.fail(ip)
        try:
            url = await billing.signup_session(
                stripe, plan=req.plan, interval=req.interval, assisted=req.assisted
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (billing.StripeError, httpx.HTTPError) as exc:
            logger.warning("Checkout failed: %s", exc)
            raise HTTPException(502, "No hemos podido abrir el pago. Prueba de nuevo.")
        return {"url": url}

    @app.post("/api/cuenta/portal")
    async def account_portal(req: AccessRequest, request: Request) -> Dict[str, str]:
        _need_stripe()
        entry = await _entry_for_code(req.code, request)
        if not entry["customer"]:
            raise HTTPException(
                409,
                "Tu Jarvis no tiene una suscripción online. "
                "Escríbenos a hola@aifasttrack.tech para gestionarla.",
            )
        try:
            url = await billing.portal_session(stripe, customer=entry["customer"])
        except (billing.StripeError, httpx.HTTPError) as exc:
            logger.warning("Portal failed: %s", exc)
            raise HTTPException(
                502, "No hemos podido abrir tu cuenta. Prueba de nuevo."
            )
        return {"url": url}

    @app.post("/api/cuenta/recarga")
    async def account_topup(req: AccessRequest, request: Request) -> Dict[str, str]:
        _need_stripe()
        entry = await _entry_for_code(req.code, request)
        if not PLANS.get(entry["plan"], PLANS["own"])["included_ai"]:
            raise HTTPException(
                409, "Tu plan usa tu propia cuenta de IA: no necesitas recargas."
            )
        try:
            url = await billing.topup_session(
                stripe, service_id=entry["id"], customer=entry["customer"]
            )
        except (billing.StripeError, httpx.HTTPError) as exc:
            logger.warning("Top-up checkout failed: %s", exc)
            raise HTTPException(502, "No hemos podido abrir el pago. Prueba de nuevo.")
        return {"url": url}

    @app.post("/api/stripe/webhook")
    async def stripe_webhook(
        request: Request, background: BackgroundTasks
    ) -> Dict[str, str]:
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(503, "Webhook no configurado")
        payload = await request.body()
        try:
            event = billing.verify_signature(
                payload, request.headers.get("stripe-signature", ""), secret
            )
        except (billing.StripeError, ValueError):
            raise HTTPException(400, "Firma no válida")
        if fulfillment.first_time(event.get("id", "")):
            # Answer Stripe at once; creating an instance takes a while.
            background.add_task(fulfillment.handle, event)
        return {"received": "1"}

    @app.get("/oauth/callback")
    async def oauth_callback(
        state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        """One redirect URI for every user's OAuth connections: bounce the
        browser back to the Jarvis named in ``state`` -- only if it is ours.

        Single domain (VPS): ``state`` names the public host itself, and the
        browser's ``jv`` cookie takes ``/oauth/done`` to the right instance
        (``/oauth/done`` is not one of the paths this site always owns)."""
        from openjarvis.integrations.manager import decode_state

        try:
            host = decode_state(state)["h"]
        except Exception:
            raise HTTPException(400, "Autorización no válida.")
        try:
            known = await asyncio.to_thread(directory.has_domain, host)
        except BackendError:
            raise HTTPException(503, "Servicio no disponible. Prueba en unos minutos.")
        if not known:
            raise HTTPException(400, "Autorización no válida.")
        params = {"state": state}
        if code:
            params["code"] = code
        if error:
            params["error"] = error[:200]
        return RedirectResponse(
            f"https://{host}/oauth/done?{urlencode(params)}", status_code=303
        )

    @app.post("/api/contacto")
    async def contact_form(
        req: contact_mail.ContactRequest, request: Request
    ) -> Dict[str, str]:
        ip = client_ip(request)
        if req.website:  # honeypot: pretend it worked
            return {"ok": "1"}
        if contact_ip.blocked(ip) or contact_all.blocked("*"):
            raise HTTPException(
                429,
                "Has enviado varios mensajes. Escríbenos a hola@aifasttrack.tech.",
            )
        try:
            data = contact_mail.validate(req)
        except contact_mail.ContactError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not contact_mail.configured():
            raise HTTPException(
                503,
                "El formulario no está disponible ahora. "
                "Escríbenos a hola@aifasttrack.tech.",
            )
        try:
            await contact_mail.send(
                contact_mail.build_payload(data, ip=ip), http=mail_http
            )
        except Exception:
            raise HTTPException(
                502,
                "No hemos podido enviar tu mensaje. "
                "Escríbenos a hola@aifasttrack.tech.",
            )
        # Every accepted message counts toward the per-IP / global ceilings.
        contact_ip.fail(ip)
        contact_all.fail("*")
        return {"ok": "1"}

    return app


__all__ = ["Directory", "create_landing_app"]
