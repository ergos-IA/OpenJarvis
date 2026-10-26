"""Contact form for the public site, delivered by email through Mailjet.

Same provider as aifasttrack.tech.  Environment:

* ``MAILJET_API_KEY`` / ``MAILJET_SECRET_KEY``  (required to send)
* ``CONTACT_TO``   recipient (default ``hola@aifasttrack.tech``)
* ``FROM_EMAIL``   sender, a Mailjet-verified address
  (default ``no-reply@aifasttrack.tech``)

The visitor's address goes in Reply-To, so answering the email answers them.
"""

from __future__ import annotations

import html
import os
import re
import time
from typing import Any, Dict, Optional

import httpx
from pydantic import BaseModel

MAILJET_URL = "https://api.mailjet.com/v3.1/send"
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class ContactRequest(BaseModel):
    name: str
    email: str
    message: str
    company: str = ""
    phone: str = ""
    consent: bool = False
    # Honeypot: hidden from people, filled in by bots.
    website: str = ""


class ContactError(ValueError):
    pass


def validate(req: ContactRequest) -> Dict[str, str]:
    data = {
        "name": " ".join(req.name.split())[:120],
        "email": req.email.strip()[:200],
        "company": " ".join(req.company.split())[:160],
        "phone": " ".join(req.phone.split())[:40],
        "message": req.message.strip()[:4000],
    }
    if not data["name"]:
        raise ContactError("Escribe tu nombre.")
    if not _EMAIL_RE.match(data["email"]):
        raise ContactError("Revisa tu email.")
    if len(data["message"]) < 10:
        raise ContactError("Cuéntanos un poco más en el mensaje.")
    if not req.consent:
        raise ContactError("Debes aceptar la política de privacidad.")
    return data


def build_payload(data: Dict[str, str], *, ip: str = "") -> Dict[str, Any]:
    to = os.environ.get("CONTACT_TO", "hola@aifasttrack.tech")
    sender = os.environ.get("FROM_EMAIL", "no-reply@aifasttrack.tech")
    esc = {k: html.escape(v) for k, v in data.items()}
    rows = [
        ("Nombre", esc["name"]),
        ("Email", esc["email"]),
        ("Empresa", esc["company"]),
        ("Teléfono", esc["phone"]),
    ]
    rows_html = "".join(
        f"<p><strong>{label}:</strong> {value}</p>" for label, value in rows if value
    )
    when = time.strftime("%d/%m/%Y %H:%M UTC", time.gmtime())
    body = (
        '<html><body style="font-family: sans-serif; padding: 20px;">'
        "<h2>Nueva solicitud desde jarvis.aifasttrack.tech</h2>"
        f"{rows_html}"
        "<p><strong>Mensaje:</strong></p>"
        f'<p style="white-space: pre-wrap">{esc["message"]}</p>'
        f'<p style="color:#888;font-size:12px">{when} · IP {html.escape(ip)} · '
        "Ha aceptado la política de privacidad.</p>"
        "</body></html>"
    )
    text = (
        "\n".join(
            f"{label}: {data[key]}"
            for label, key in (
                ("Nombre", "name"),
                ("Email", "email"),
                ("Empresa", "company"),
                ("Teléfono", "phone"),
            )
            if data[key]
        )
        + f"\n\nMensaje:\n{data['message']}\n"
    )
    return {
        "Messages": [
            {
                "From": {"Email": sender, "Name": "Jarvis · AI Fast Track"},
                "To": [{"Email": to, "Name": "AI FAST TRACK Team"}],
                "ReplyTo": {"Email": data["email"], "Name": data["name"]},
                "Subject": f"Jarvis · Nueva solicitud: {data['name']}",
                "TextPart": text,
                "HTMLPart": body,
            }
        ]
    }


def configured() -> bool:
    return bool(
        os.environ.get("MAILJET_API_KEY") and os.environ.get("MAILJET_SECRET_KEY")
    )


async def send(
    payload: Dict[str, Any], *, http: Optional[httpx.AsyncClient] = None
) -> None:
    auth = (os.environ["MAILJET_API_KEY"], os.environ["MAILJET_SECRET_KEY"])
    if http is None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(MAILJET_URL, json=payload, auth=auth)
    else:
        resp = await http.post(MAILJET_URL, json=payload, auth=auth)
    if resp.status_code >= 300:
        raise RuntimeError(f"Mailjet {resp.status_code}: {resp.text[:200]}")


__all__ = ["ContactError", "ContactRequest", "build_payload", "configured", "send"]
