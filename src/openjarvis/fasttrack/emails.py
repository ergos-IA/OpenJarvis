# ruff: noqa: E501  (inline-styled HTML email templates)
"""Customer emails (Mailjet), with one elegant AI Fast Track layout.

* :func:`welcome`      -- right after paying: we are preparing your Jarvis.
* :func:`access_ready` -- sent by the customer's own instance the first time
  it is up (see :func:`send_access_email_once`): access code and link.
* :func:`cancelled`, :func:`payment_failed` -- billing notices.

Each returns ``(subject, text, html)``; :func:`payload` wraps them for Mailjet.
HTML is table-based with inline styles so it renders in Gmail and Outlook.
"""

from __future__ import annotations

import html
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

Email = Tuple[str, str, str]

_BG = "#0a0c0e"
_CARD = "#111417"
_LINE = "#232a31"
_T1 = "#f1f4f6"
_T2 = "#b4bec7"
_T3 = "#7d8893"
_YELLOW = "#ffdb05"
_CYAN = "#00d9ff"
_FONT = "'Helvetica Neue',Helvetica,Arial,sans-serif"
_MONO = "'SFMono-Regular',Menlo,Consolas,'Courier New',monospace"


def site_url() -> str:
    return os.environ.get("PUBLIC_URL", "https://jarvis.aifasttrack.tech").rstrip("/")


def _e(text: str) -> str:
    return html.escape(str(text or ""))


def _layout(
    *,
    preheader: str,
    eyebrow: str,
    title: str,
    blocks: List[str],
) -> str:
    company = os.environ.get("LEGAL_COMPANY", "Fast Factory IA Engine S.L.")
    site = site_url()
    body = "".join(blocks)
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><meta name="supported-color-schemes" content="dark light">
<title>{_e(title)}</title></head>
<body style="margin:0;padding:0;background:{_BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{_BG};">{_e(preheader)}&#8199;&#65279;&#847;&#8199;&#65279;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};">
<tr><td align="center" style="padding:32px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">
  <tr><td style="padding:0 4px 18px 4px;">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="border:2px solid {_T1};border-radius:6px;padding:2px 6px;font:700 15px {_FONT};color:{_T1};">AI</td>
      <td style="padding-left:8px;font:800 13px/1 {_FONT};color:{_YELLOW};letter-spacing:.5px;">FAST<br><span style="color:{_T1};font-weight:700;font-size:11px;">TRACK</span></td>
      <td style="padding-left:14px;border-left:1px solid {_LINE};font:600 11px {_MONO};letter-spacing:3px;color:{_T3};">&nbsp;&nbsp;JARVIS</td>
    </tr></table>
  </td></tr>
  <tr><td style="background:{_CARD};border:1px solid {_LINE};border-top:3px solid {_CYAN};padding:36px 32px 32px 32px;">
    <div style="font:600 11px {_MONO};letter-spacing:3px;color:{_CYAN};text-transform:uppercase;">{_e(eyebrow)}</div>
    <h1 style="margin:14px 0 18px 0;font:500 30px/1.15 {_FONT};color:{_T1};letter-spacing:-.5px;">{_e(title)}</h1>
    {body}
  </td></tr>
  <tr><td style="padding:22px 6px 0 6px;font:12px/1.6 {_FONT};color:{_T3};">
    Jarvis es un servicio de AI Fast Track · {_e(company)}<br>
    <a href="{site}" style="color:{_T3};">{_e(site.replace("https://", ""))}</a> ·
    <a href="{site}/cuenta" style="color:{_T3};">Mi cuenta</a> ·
    <a href="{site}/legal/terminos" style="color:{_T3};">Términos</a> ·
    <a href="{site}/legal/privacidad" style="color:{_T3};">Privacidad</a><br>
    ¿Dudas? Responde a este email y te ayudamos.
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def _p(text: str) -> str:
    return f'<p style="margin:0 0 16px 0;font:16px/1.6 {_FONT};color:{_T2};">{text}</p>'


def _button(label: str, url: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px 0;"><tr>'
        f'<td style="background:{_YELLOW};"><a href="{_e(url)}" '
        f'style="display:inline-block;padding:16px 26px;font:700 13px {_MONO};letter-spacing:2px;'
        f'color:#040506;text-decoration:none;text-transform:uppercase;">{_e(label)} &rarr;</a></td>'
        "</tr></table>"
    )


def _code(code: str) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 22px 0;">'
        f'<tr><td style="border:1px solid {_LINE};background:#07090b;padding:20px;text-align:center;">'
        f'<div style="font:600 11px {_MONO};letter-spacing:3px;color:{_T3};">TU CÓDIGO DE ACCESO</div>'
        f'<div style="margin-top:10px;font:700 30px {_MONO};letter-spacing:6px;color:{_YELLOW};">{_e(code)}</div>'
        "</td></tr></table>"
    )


def _steps(items: List[str]) -> str:
    rows = "".join(
        f'<tr><td valign="top" style="padding:0 14px 14px 0;font:700 13px {_MONO};color:{_CYAN};">'
        f"{i:02d}</td>"
        f'<td style="padding:0 0 14px 0;font:15px/1.55 {_FONT};color:{_T2};">{item}</td></tr>'
        for i, item in enumerate(items, 1)
    )
    return f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 12px 0;">{rows}</table>'


def _note(text: str) -> str:
    return (
        f'<div style="margin:8px 0 0 0;padding:14px 16px;border-left:3px solid {_YELLOW};'
        f'background:rgba(255,219,5,.06);font:14px/1.55 {_FONT};color:{_T2};">{text}</div>'
    )


def _plain(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p) + "\n\n— El equipo de AI Fast Track"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def welcome(
    name: str, plan_label: str, *, assisted: bool = False, trial_days: int = 30
) -> Email:
    first = (name or "").split(" ")[0] or "hola"
    subject = "Bienvenido a Jarvis: estamos preparando tu asistente"
    extra = (
        "Has elegido el <b style='color:#f1f4f6'>alta asistida</b>: te escribiremos en las "
        "próximas 24 h laborables para dejarlo todo funcionando contigo."
        if assisted
        else ""
    )
    blocks = [
        _p(f"Hola {_e(first)}:"),
        _p(
            f"Gracias por elegir Jarvis, plan <b style='color:{_T1}'>{_e(plan_label)}</b>. "
            "Ahora mismo estamos creando tu entorno privado: un asistente solo para ti, "
            "con tus datos separados de los de cualquier otra persona."
        ),
        _p(
            "<b style='color:#f1f4f6'>En cuanto esté listo (normalmente en menos de 20 minutos) "
            "te enviaremos un segundo email con tu código y tu enlace de acceso.</b> "
            "No tienes que hacer nada mientras tanto."
        ),
        f'<div style="margin:22px 0 10px 0;font:600 11px {_MONO};letter-spacing:3px;color:{_T3};">LO QUE VIENE DESPUÉS</div>',
        _steps(
            [
                "Recibirás el email <b style='color:#f1f4f6'>«Tu Jarvis está listo»</b> con tu acceso.",
                "Aceptas las condiciones y, si tu plan lo pide, conectas tu cuenta de IA (con tutorial).",
                "Vinculas tu WhatsApp en un minuto, y si quieres tu correo y tus herramientas.",
            ]
        ),
        _note(
            f"Los primeros {trial_days} días son gratis. Puedes cancelar antes desde "
            f"<a href='{site_url()}/cuenta' style='color:{_CYAN};'>Mi cuenta</a> y no se cobrará nada."
        ),
    ]
    if extra:
        blocks.append(_note(extra))
    text = _plain(
        f"Hola {first}:",
        f"Gracias por elegir Jarvis, plan {plan_label}. Estamos creando tu entorno privado.",
        "En cuanto esté listo (normalmente en menos de 20 minutos) te enviaremos un "
        "segundo email con tu código y tu enlace de acceso. No tienes que hacer nada "
        "mientras tanto.",
        f"Los primeros {trial_days} días son gratis; puedes cancelar antes en "
        f"{site_url()}/cuenta.",
        "Has elegido el alta asistida: te escribiremos en las próximas 24 h laborables."
        if assisted
        else "",
    )
    html_body = _layout(
        preheader="Estamos preparando tu asistente. En unos minutos te llega el acceso.",
        eyebrow="Bienvenido",
        title="Tu Jarvis se está preparando",
        blocks=blocks,
    )
    return subject, text, html_body


def access_ready(
    name: str,
    *,
    code: str,
    link: str = "",
    assistant: str = "Jarvis",
    own_ai: bool = False,
) -> Email:
    first = (name or "").split(" ")[0] or "hola"
    site = site_url()
    subject = f"{assistant} está listo: aquí tienes tu acceso"
    ai_step = (
        "Conecta tu cuenta de IA (Claude, OpenAI…): el asistente te guía paso a paso."
        if own_ai
        else "Tu plan ya incluye la inteligencia artificial: no tienes que configurar nada."
    )
    blocks = [
        _p(f"Hola {_e(first)}:"),
        _p(
            f"Tu asistente <b style='color:{_T1}'>{_e(assistant)}</b> ya está en marcha. "
            "Entra para terminar la configuración: son unos minutos."
        ),
        _button("Abrir mi Jarvis", link or site),
        _code(code),
        _p(
            f"También puedes entrar desde <a href='{site}' style='color:{_CYAN};'>"
            f"{_e(site.replace('https://', ''))}</a> → «Acceder» con tu código. "
            "Guárdalo y no lo compartas: abre tu asistente."
        ),
        f'<div style="margin:22px 0 10px 0;font:600 11px {_MONO};letter-spacing:3px;color:{_T3};">PRIMEROS PASOS</div>',
        _steps(
            [
                "Acepta las condiciones de uso.",
                _e(ai_step),
                "Vincula tu WhatsApp escaneando un código (o con un código de 8 letras desde el móvil).",
                "Opcional: conecta tu correo de Gmail u Outlook y herramientas como tu calendario, Notion o Todoist.",
            ]
        ),
        _note(
            "Para empezar, escríbete a ti mismo en WhatsApp (el chat «Tú») algo como "
            "<i>«¿Qué tengo mañana?»</i> o <i>«Recuérdame llamar a Ana a las 5»</i>."
        ),
    ]
    text = _plain(
        f"Hola {first}:",
        f"Tu asistente {assistant} ya está en marcha.",
        f"Ábrelo aquí: {link or site}",
        f"O entra en {site} → «Acceder» con tu código: {code}",
        "Primeros pasos: acepta las condiciones, "
        + ai_step[0].lower()
        + ai_step[1:]
        + " Vincula tu WhatsApp y, si quieres, tu correo y tus herramientas.",
    )
    html_body = _layout(
        preheader=f"Tu código de acceso: {code}",
        eyebrow="Acceso listo",
        title=f"{assistant} está listo para ti",
        blocks=blocks,
    )
    return subject, text, html_body


def cancelled(name: str) -> Email:
    first = (name or "").split(" ")[0] or "hola"
    subject = "Tu suscripción a Jarvis ha terminado"
    blocks = [
        _p(f"Hola {_e(first)}:"),
        _p(
            "Tu suscripción a Jarvis se ha cancelado. En los próximos días borraremos tu "
            "asistente y sus datos, y se desvinculará tu WhatsApp."
        ),
        _p("Si ha sido un error o quieres volver, responde a este email."),
        _p("Gracias por probar Jarvis."),
    ]
    text = _plain(
        f"Hola {first}:",
        "Tu suscripción a Jarvis se ha cancelado. En los próximos días borraremos tu "
        "asistente y sus datos, y se desvinculará tu WhatsApp.",
        "Si ha sido un error o quieres volver, responde a este email.",
    )
    return (
        subject,
        text,
        _layout(
            preheader="Hemos cancelado tu suscripción.",
            eyebrow="Baja",
            title="Hasta pronto",
            blocks=blocks,
        ),
    )


def payment_failed(name: str, link: str) -> Email:
    first = (name or "").split(" ")[0] or "hola"
    subject = "No hemos podido cobrar tu suscripción a Jarvis"
    blocks = [
        _p(f"Hola {_e(first)}:"),
        _p(
            "El último cobro de tu suscripción no se ha podido completar. Suele pasar "
            "con tarjetas caducadas o sin saldo."
        ),
        _button("Revisar el pago", link),
        _p(
            "Lo volveremos a intentar en unos días. Si ya lo has resuelto, ignora este email."
        ),
    ]
    text = _plain(
        f"Hola {first}:",
        "El cobro de tu suscripción a Jarvis no se ha podido completar.",
        f"Puedes pagarlo o cambiar la tarjeta aquí: {link}",
        "Volveremos a intentarlo en unos días.",
    )
    return (
        subject,
        text,
        _layout(
            preheader="Revisa tu medio de pago.",
            eyebrow="Pago pendiente",
            title="Hay un problema con el pago",
            blocks=blocks,
        ),
    )


def payload(to: str, name: str, email: Email) -> Dict[str, Any]:
    subject, text, html_body = email
    return {
        "Messages": [
            {
                "From": {
                    "Email": os.environ.get("FROM_EMAIL", "no-reply@aifasttrack.tech"),
                    "Name": "Jarvis · AI Fast Track",
                },
                "To": [{"Email": to, "Name": name or to}],
                "ReplyTo": {
                    "Email": os.environ.get("CONTACT_TO", "hola@aifasttrack.tech"),
                    "Name": "AI Fast Track",
                },
                "Subject": subject,
                "TextPart": text,
                "HTMLPart": html_body,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Sent by the customer's instance once it is up
# ---------------------------------------------------------------------------


def send_access_email_once(marker: Optional[Path] = None, *, send=None) -> bool:
    """If this instance was created by a self-service sign-up, email the
    customer their access the first time it starts.  Returns True if sent."""
    if os.environ.get("JARVIS_SEND_ACCESS_EMAIL") != "1":
        return False
    to = os.environ.get("JARVIS_USER_EMAIL", "").strip()
    code = os.environ.get("JARVIS_ACCESS_CODE", "").strip()
    if not to or not code:
        return False
    if marker is None:
        from openjarvis.core.paths import get_config_dir

        marker = get_config_dir() / "access_email_sent"
    if marker.exists():
        return False
    from openjarvis.fasttrack import contact

    if send is None and not contact.configured():
        logger.warning("Access email not sent: Mailjet is not configured")
        return False
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    key = os.environ.get("OPENJARVIS_API_KEY", "")
    link = f"https://{domain}/#key={key}" if domain and key else ""
    from openjarvis.fasttrack.plans import plan

    name = os.environ.get("JARVIS_USER_NAME", "")
    email = access_ready(
        name,
        code=code,
        link=link,
        assistant=os.environ.get("JARVIS_ASSISTANT_NAME", "Jarvis") or "Jarvis",
        own_ai=not plan()["included_ai"],
    )
    message = payload(to, name, email)
    try:
        if send is not None:
            send(message)
        else:
            import asyncio

            asyncio.run(contact.send(message))
    except Exception:
        logger.exception("Access email failed; will retry on next start")
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("sent", encoding="utf-8")
    logger.info("Access email sent to the customer")
    return True


__all__ = [
    "access_ready",
    "cancelled",
    "payload",
    "payment_failed",
    "send_access_email_once",
    "site_url",
    "welcome",
]
