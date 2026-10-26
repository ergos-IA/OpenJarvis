"""Self-service sign-up and payments with Stripe (public site only).

* ``POST /api/alta``: Stripe Checkout for a plan (monthly/yearly, first month
  free, optional assisted set-up).  Prices are looked up by ``lookup_key``
  (``jarvis_<plan>_<month|year>``, ``jarvis_setup``, ``jarvis_topup``), VAT
  included, Stripe Tax on.
* ``POST /api/stripe/webhook``: signed events.  A completed sign-up creates the
  customer's Jarvis (:func:`provisioning.provision_user`) and emails them their
  access code; a top-up adds credits to their instance; cancellations and
  failed payments are emailed to the customer and to the team.
* ``POST /api/cuenta/portal`` / ``/api/cuenta/recarga``: from the customer's
  access code, open the Stripe customer portal (card, invoices, cancel) or
  buy a top-up.

Environment: ``STRIPE_SECRET_KEY``, ``STRIPE_WEBHOOK_SECRET``,
``STRIPE_PORTAL_CONFIGURATION`` (customer portal settings, optional),
``FASTTRACK_ADMIN_TOKEN`` (top-ups), ``PUBLIC_URL`` (this site), plus the
Railway token and Mailjet keys the site already uses.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from openjarvis.fasttrack import contact as mail
from openjarvis.fasttrack import emails
from openjarvis.fasttrack.models import DEFAULT_MODEL
from openjarvis.fasttrack.plans import PLANS, TOPUP_CREDITS
from openjarvis.fasttrack.provisioning import provision_user

logger = logging.getLogger(__name__)

STRIPE_API = "https://api.stripe.com/v1/"
TRIAL_DAYS = 30
# Shared variables a self-service instance needs to email its access itself.
INSTANCE_MAIL_VARS = (
    "MAILJET_API_KEY",
    "MAILJET_SECRET_KEY",
    "FROM_EMAIL",
    "CONTACT_TO",
)
INTERVALS = ("month", "year")


class StripeError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def public_url() -> str:
    return os.environ.get("PUBLIC_URL", "https://jarvis.aifasttrack.tech").rstrip("/")


def _form(data: Any, prefix: str = "") -> List[Tuple[str, str]]:
    """Stripe's nested form encoding: a[b][0][c]=v."""
    items: List[Tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            items += _form(value, f"{prefix}[{key}]" if prefix else str(key))
    elif isinstance(data, (list, tuple)):
        for i, value in enumerate(data):
            items += _form(value, f"{prefix}[{i}]")
    elif isinstance(data, bool):
        items.append((prefix, "true" if data else "false"))
    elif data is not None:
        items.append((prefix, str(data)))
    return items


class StripeClient:
    def __init__(
        self, key: Optional[str] = None, *, http: Optional[httpx.AsyncClient] = None
    ) -> None:
        self._key = key
        self._http = http
        self._prices: Dict[str, str] = {}

    async def call(
        self, method: str, path: str, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        key = self._key or os.environ.get("STRIPE_SECRET_KEY", "")
        if not key:
            raise StripeError("Stripe no está configurado")
        kwargs: Dict[str, Any] = {"auth": (key, "")}
        if method == "GET":
            kwargs["params"] = _form(data or {})
        else:
            kwargs["data"] = dict(_form(data or {}))
        if self._http is None:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.request(method, STRIPE_API + path, **kwargs)
        else:
            resp = await self._http.request(method, STRIPE_API + path, **kwargs)
        body = resp.json()
        if resp.status_code >= 300:
            message = (body.get("error") or {}).get("message", resp.text[:200])
            raise StripeError(message)
        return body

    async def price(self, lookup_key: str) -> str:
        if lookup_key not in self._prices:
            found = await self.call(
                "GET", "prices", {"lookup_keys": [lookup_key], "active": True}
            )
            if not found.get("data"):
                raise StripeError(f"Falta el precio {lookup_key} en Stripe")
            self._prices[lookup_key] = found["data"][0]["id"]
        return self._prices[lookup_key]


def verify_signature(
    payload: bytes,
    header: str,
    secret: str,
    *,
    tolerance_s: int = 300,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Check the ``Stripe-Signature`` header and return the event."""
    parts: Dict[str, List[str]] = {}
    for item in (header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError):
        raise StripeError("Firma de Stripe no válida")
    expected = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise StripeError("Firma de Stripe no válida")
    if abs((now or time.time()) - ts) > tolerance_s:
        raise StripeError("Evento de Stripe caducado")
    return json.loads(payload)


# ---------------------------------------------------------------------------
# Checkout sessions
# ---------------------------------------------------------------------------


async def signup_session(
    stripe: StripeClient, *, plan: str, interval: str, assisted: bool = False
) -> str:
    if plan not in PLANS:
        raise ValueError("Plan desconocido")
    if interval not in INTERVALS:
        raise ValueError("Periodo desconocido")
    items = [{"price": await stripe.price(f"jarvis_{plan}_{interval}"), "quantity": 1}]
    if assisted:
        items.append({"price": await stripe.price("jarvis_setup"), "quantity": 1})
    base = public_url()
    session = await stripe.call(
        "POST",
        "checkout/sessions",
        {
            "mode": "subscription",
            "line_items": items,
            "subscription_data": {
                "trial_period_days": TRIAL_DAYS,
                "metadata": {"plan": plan},
            },
            "metadata": {"kind": "signup", "plan": plan, "assisted": int(assisted)},
            "automatic_tax": {"enabled": True},
            "billing_address_collection": "required",
            "tax_id_collection": {"enabled": True},
            "allow_promotion_codes": True,
            "locale": "es",
            "custom_fields": [
                {
                    "key": "assistant",
                    "label": {
                        "type": "custom",
                        "custom": "Nombre de tu asistente (opcional)",
                    },
                    "type": "text",
                    "optional": True,
                }
            ],
            "custom_text": {
                "submit": {
                    "message": (
                        f"Los primeros {TRIAL_DAYS} días son gratis y puedes "
                        "cancelar antes sin coste. Al suscribirte aceptas los "
                        f"Términos ({base}/legal/terminos) y la Política de "
                        f"privacidad ({base}/legal/privacidad)."
                    )
                }
            },
            "success_url": f"{base}/?alta=ok",
            "cancel_url": f"{base}/#precios",
        },
    )
    return session["url"]


async def topup_session(stripe: StripeClient, *, service_id: str, customer: str) -> str:
    base = public_url()
    data: Dict[str, Any] = {
        "mode": "payment",
        "line_items": [{"price": await stripe.price("jarvis_topup"), "quantity": 1}],
        "metadata": {"kind": "topup", "service_id": service_id},
        "automatic_tax": {"enabled": True},
        "invoice_creation": {"enabled": True},
        "locale": "es",
        "success_url": f"{base}/cuenta?recarga=ok",
        "cancel_url": f"{base}/cuenta",
    }
    if customer:
        data["customer"] = customer
        data["customer_update"] = {"address": "auto", "name": "auto"}
    else:
        data["billing_address_collection"] = "required"
    return (await stripe.call("POST", "checkout/sessions", data))["url"]


async def portal_session(stripe: StripeClient, *, customer: str) -> str:
    session = await stripe.call(
        "POST",
        "billing_portal/sessions",
        {
            "customer": customer,
            "return_url": f"{public_url()}/cuenta",
            # Invoices, card, address/NIF and cancel at period end; plan
            # changes go through us (they must also change the instance).
            "configuration": os.environ.get("STRIPE_PORTAL_CONFIGURATION") or None,
        },
    )
    return session["url"]


# ---------------------------------------------------------------------------
# Webhook fulfilment
# ---------------------------------------------------------------------------

Lookup = Callable[..., Optional[Dict[str, str]]]


def _email(to: str, subject: str, text: str, *, name: str = "") -> Dict[str, Any]:
    sender = os.environ.get("FROM_EMAIL", "no-reply@aifasttrack.tech")
    body = "".join(
        f"<p>{html.escape(p)}</p>".replace("\n", "<br>") for p in text.split("\n\n")
    )
    return {
        "Messages": [
            {
                "From": {"Email": sender, "Name": "Jarvis · AI Fast Track"},
                "To": [{"Email": to, "Name": name or to}],
                "ReplyTo": {
                    "Email": os.environ.get("CONTACT_TO", "hola@aifasttrack.tech")
                },
                "Subject": subject,
                "TextPart": text,
                "HTMLPart": (
                    '<html><body style="font-family: sans-serif; line-height: 1.5; '
                    f'padding: 20px; max-width: 560px">{body}</body></html>'
                ),
            }
        ]
    }


class Fulfillment:
    """Acts on Stripe events.  Railway/Mailjet calls are injected for tests."""

    def __init__(
        self,
        rw: Any,
        lookup: Lookup,
        *,
        send: Optional[Callable[[Dict[str, Any]], Any]] = None,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._rw = rw
        self._lookup = lookup
        self._send = send or mail.send
        self._http = http
        self._seen: "OrderedDict[str, float]" = OrderedDict()

    def first_time(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        self._seen[event_id] = time.time()
        while len(self._seen) > 1000:
            self._seen.popitem(last=False)
        return True

    async def _mail(self, to: str, subject: str, text: str, name: str = "") -> None:
        """Plain internal email (to the team)."""
        await self._deliver(to, subject, _email(to, subject, text, name=name))

    async def _customer(self, to: str, name: str, email: emails.Email) -> None:
        """Designed customer email (see :mod:`openjarvis.fasttrack.emails`)."""
        await self._deliver(to, email[0], emails.payload(to, name, email))

    async def _deliver(self, to: str, subject: str, message: Dict[str, Any]) -> None:
        if not to or not mail.configured():
            logger.warning("Email not sent (%s): %s", to or "no address", subject)
            return
        try:
            await self._send(message)
        except Exception:
            logger.exception("Email failed: %s", subject)

    async def _team(self, subject: str, text: str) -> None:
        await self._mail(
            os.environ.get("CONTACT_TO", "hola@aifasttrack.tech"),
            f"Jarvis · {subject}",
            text,
        )

    async def handle(self, event: Dict[str, Any]) -> None:
        kind = event.get("type", "")
        obj = (event.get("data") or {}).get("object") or {}
        try:
            if kind == "checkout.session.completed":
                meta = obj.get("metadata") or {}
                if meta.get("kind") == "topup":
                    await self._topup(obj, meta)
                elif meta.get("kind") == "signup":
                    await self._signup(obj, meta)
            elif kind == "customer.subscription.deleted":
                await self._cancelled(obj)
            elif kind == "invoice.payment_failed":
                await self._payment_failed(obj)
        except Exception as exc:
            logger.exception("Stripe event %s failed", event.get("id"))
            await self._team(
                "ERROR procesando un pago",
                f"Evento {event.get('id')} ({kind}) ha fallado: {exc}\n\n"
                "Revísalo en Stripe y, si hace falta, da de alta al cliente "
                "desde la consola.",
            )

    async def _signup(self, session: Dict[str, Any], meta: Dict[str, str]) -> None:
        customer = session.get("customer") or ""
        if customer and await asyncio.to_thread(self._lookup, customer=customer):
            return  # already provisioned (Stripe retried the event)
        details = session.get("customer_details") or {}
        email = details.get("email") or ""
        name = details.get("name") or email.split("@")[0] or "Cliente"
        plan = meta.get("plan") if meta.get("plan") in PLANS else "own"
        assistant = "Jarvis"
        for field in session.get("custom_fields") or []:
            if field.get("key") == "assistant":
                assistant = ((field.get("text") or {}).get("value") or "").strip()[
                    :40
                ] or "Jarvis"
        included = PLANS[plan]["included_ai"]
        model = PLANS[plan]["models"][0] if included else DEFAULT_MODEL
        result = await asyncio.to_thread(
            provision_user,
            self._rw,
            name=name,
            email=email,
            model=model,
            assistant_name=assistant,
            shared_ai=included,
            extra_vars={
                "JARVIS_PLAN": plan,
                "STRIPE_CUSTOMER_ID": customer,
                "STRIPE_SUBSCRIPTION_ID": session.get("subscription") or "",
                # The instance emails the access itself once it is up.
                "JARVIS_SEND_ACCESS_EMAIL": "1",
                **{var: "${{shared.%s}}" % var for var in INSTANCE_MAIL_VARS},
                "PUBLIC_URL": public_url(),
            },
        )
        assisted = meta.get("assisted") == "1"
        await self._customer(
            email,
            name,
            emails.welcome(
                name, PLANS[plan]["label"], assisted=assisted, trial_days=TRIAL_DAYS
            ),
        )
        await self._team(
            f"Nueva alta: {name} ({PLANS[plan]['label']})",
            f"Cliente: {name} <{email}>\nPlan: {PLANS[plan]['label']}\n"
            f"Servicio: {result['service']}\nAlta asistida: "
            f"{'SÍ, contactar' if assisted else 'no'}\n"
            f"Error de build: {result['build_error'] or 'ninguno'}",
        )

    async def _topup(self, session: Dict[str, Any], meta: Dict[str, str]) -> None:
        sid = meta.get("service_id", "")
        variables, domain = await asyncio.gather(
            asyncio.to_thread(self._rw.variables, sid),
            asyncio.to_thread(self._rw.domain, sid),
        )
        token = os.environ.get("FASTTRACK_ADMIN_TOKEN", "")
        headers = {
            "Authorization": f"Bearer {variables.get('OPENJARVIS_API_KEY', '')}",
            "X-FastTrack-Admin": token,
        }
        url = f"https://{domain}/api/credits/topup"
        payload = {"credits": TOPUP_CREDITS}
        if self._http is None:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, json=payload, headers=headers)
        else:
            resp = await self._http.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"El Jarvis rechazó la recarga ({resp.status_code})")

    async def _cancelled(self, sub: Dict[str, Any]) -> None:
        entry = await asyncio.to_thread(self._lookup, customer=sub.get("customer", ""))
        name = (entry or {}).get("name", "")
        email = (entry or {}).get("email", "")
        await self._team(
            f"Baja: {name or sub.get('customer')}",
            f"La suscripción {sub.get('id')} se ha cancelado.\n"
            f"Servicio: {(entry or {}).get('service', 'no encontrado')}\n\n"
            "Elimina su instancia desde la consola cuando corresponda.",
        )
        await self._customer(email, name, emails.cancelled(name))

    async def _payment_failed(self, invoice: Dict[str, Any]) -> None:
        email = invoice.get("customer_email") or ""
        name = invoice.get("customer_name") or ""
        link = invoice.get("hosted_invoice_url") or f"{public_url()}/cuenta"
        await self._customer(email, name, emails.payment_failed(name, link))
        await self._team(
            f"Pago fallido: {name or email}",
            f"Factura {invoice.get('id')} de {email}: cobro fallido.",
        )


__all__ = [
    "Fulfillment",
    "StripeClient",
    "StripeError",
    "TRIAL_DAYS",
    "configured",
    "portal_session",
    "signup_session",
    "topup_session",
    "verify_signature",
]
