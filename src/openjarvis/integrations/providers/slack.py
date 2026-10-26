# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Slack through a user token (xoxp-) from an app created from a manifest link."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from openjarvis.integrations.base import (
    Field,
    I,
    Integration,
    IntegrationError,
    S,
    register,
    request,
    tool,
)

API = "https://slack.com/api"
USER_SCOPES = [
    "search:read",
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:read",
    "users:read",
    "chat:write",
]
MANIFEST = {
    "_metadata": {"major_version": 2},
    "display_information": {
        "name": "Jarvis",
        "description": "Tu asistente personal en WhatsApp",
    },
    "oauth_config": {"scopes": {"user": USER_SCOPES}},
}
MANIFEST_URL = "https://api.slack.com/apps?new_app=1&manifest_json=" + quote(
    json.dumps(MANIFEST, separators=(",", ":"))
)
AUTH_ERROR = (
    "Slack no acepta el token. Copia de nuevo el «User OAuth Token» (empieza "
    "por xoxp-) desde la página de tu app → OAuth & Permissions."
)
_ERRORS = {
    "invalid_auth": AUTH_ERROR,
    "not_authed": AUTH_ERROR,
    "token_revoked": AUTH_ERROR,
    "account_inactive": AUTH_ERROR,
    "missing_scope": "Al token le falta un permiso. Crea la app con el enlace de la guía (incluye todos los permisos) y reinstálala.",
    "channel_not_found": "No encuentro ese canal (o no tienes acceso a él).",
    "not_in_channel": "No estás dentro de ese canal; únete en Slack y vuelve a intentarlo.",
    "is_archived": "Ese canal está archivado.",
    "ratelimited": "Slack pide esperar un poco (límite de uso).",
    "msg_too_long": "El mensaje es demasiado largo.",
}
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")
_USER_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")


def _token(creds: Dict[str, str]) -> str:
    token = (creds.get("token") or "").strip()
    if token.startswith("xoxb-"):
        raise IntegrationError(
            "Ese es el token del bot (empieza por xoxb-). Necesito el «User OAuth Token», "
            "el que empieza por xoxp-: está justo encima en la misma página "
            "(OAuth & Permissions)."
        )
    if not token.startswith("xoxp-"):
        raise IntegrationError(
            "El token debe empezar por xoxp-. Cópialo de «User OAuth Token» en la página "
            "de tu app de Slack → OAuth & Permissions."
        )
    return token


def _call(
    token: str, method: str, *, post: bool = False, **params: Any
) -> Dict[str, Any]:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    headers = {"Authorization": f"Bearer {token}"}
    if post:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = request(
            "POST",
            f"{API}/{method}",
            headers=headers,
            json=params,
            auth_error=AUTH_ERROR,
        )
    else:
        data = request(
            "GET",
            f"{API}/{method}",
            headers=headers,
            params=params,
            auth_error=AUTH_ERROR,
        )
    if not isinstance(data, dict):
        raise IntegrationError("Slack respondió algo inesperado.")
    if not data.get("ok"):
        err = data.get("error", "desconocido")
        raise IntegrationError(_ERRORS.get(err, f"Slack respondió con un error: {err}"))
    return data


def _when(ts: Any) -> str:
    try:
        from openjarvis.personal.reminders import local_tz

        return datetime.fromtimestamp(float(ts), local_tz()).strftime("%d/%m %H:%M")
    except Exception:
        return ""


def _check(creds: Dict[str, str]) -> str:
    me = _call(_token(creds), "auth.test")
    team, user = me.get("team", ""), me.get("user", "")
    return f"{team} · {user}" if team and user else team or user or "Slack"


def _tools(creds: Dict[str, str]):
    token = _token(creds)
    cache: Dict[str, Any] = {}

    def call(method: str, **params: Any) -> Dict[str, Any]:
        return _call(token, method, **params)

    def users() -> Dict[str, Dict[str, Any]]:
        if "users" not in cache:
            members = call("users.list", limit=1000).get("members", [])
            cache["users"] = {
                m["id"]: m for m in members if not m.get("deleted") and m.get("id")
            }
        return cache["users"]

    def user_name(uid: Optional[str]) -> str:
        if not uid:
            return ""
        try:
            m = users().get(uid) or {}
        except IntegrationError:
            return uid
        prof = m.get("profile") or {}
        return prof.get("display_name") or m.get("real_name") or m.get("name") or uid

    def channels(types: str = "public_channel,private_channel") -> List[Dict[str, Any]]:
        key = f"ch:{types}"
        if key not in cache:
            cache[key] = call(
                "conversations.list", types=types, exclude_archived="true", limit=1000
            ).get("channels", [])
        return cache[key]

    def find_user(name: str) -> str:
        name = name.lstrip("@").strip().lower()
        for uid, m in users().items():
            prof = m.get("profile") or {}
            names = {
                (m.get("name") or "").lower(),
                (m.get("real_name") or "").lower(),
                (prof.get("display_name") or "").lower(),
                (prof.get("real_name") or "").lower(),
            }
            if name in names:
                return uid
        raise IntegrationError(f"No encuentro a la persona «{name}» en Slack.")

    def channel_id(value: str) -> str:
        value = (value or "").strip()
        if _CHANNEL_ID.match(value):
            return value
        name = value.lstrip("#").lower()
        for ch in channels():
            if (ch.get("name") or "").lower() == name:
                return ch["id"]
        raise IntegrationError(
            f"No encuentro el canal «{value}». Usa slack_list_channels para ver los nombres."
        )

    @tool(
        "slack_search",
        "Busca mensajes en Slack (admite modificadores de Slack como «in:#canal», "
        "«from:@persona», «after:2025-01-31»).",
        {"texto": S("Qué buscar"), "limite": I("Máximo de resultados (1-30)")},
        ["texto"],
        integration="slack",
    )
    def slack_search(texto: str, limite: int = 15) -> Any:
        data = call(
            "search.messages",
            query=texto,
            count=max(1, min(int(limite or 15), 30)),
            sort="timestamp",
        )
        matches = (data.get("messages") or {}).get("matches", [])
        if not matches:
            return "No he encontrado mensajes con esa búsqueda."
        out = []
        for m in matches:
            ch = m.get("channel") or {}
            out.append(
                {
                    "fecha": _when(m.get("ts")),
                    "de": m.get("username") or user_name(m.get("user")),
                    "canal": ("#" + ch["name"]) if ch.get("name") else "",
                    "texto": (m.get("text") or "")[:400],
                    "enlace": m.get("permalink", ""),
                }
            )
        return out

    @tool(
        "slack_read_channel",
        "Lee los últimos mensajes de un canal de Slack.",
        {
            "canal": S("Nombre del canal (con o sin #) o su ID"),
            "limite": I("Cuántos mensajes (1-50, por defecto 20)"),
        },
        ["canal"],
        integration="slack",
    )
    def slack_read_channel(canal: str, limite: int = 20) -> str:
        data = call(
            "conversations.history",
            channel=channel_id(canal),
            limit=max(1, min(int(limite or 20), 50)),
        )
        msgs = data.get("messages", [])
        if not msgs:
            return "No hay mensajes recientes en ese canal."
        lines = []
        for m in reversed(msgs):  # oldest first, reads like a chat
            who = (
                m.get("user_profile", {}).get("display_name")
                or user_name(m.get("user"))
                or m.get("username")
                or "bot"
            )
            text = (m.get("text") or "").replace("\n", " ")[:400]
            extra = f" ({m['reply_count']} respuestas)" if m.get("reply_count") else ""
            lines.append(f"{_when(m.get('ts'))} {who}: {text}{extra}")
        return "\n".join(lines)

    @tool(
        "slack_list_channels",
        "Lista los canales de Slack del usuario (públicos y privados en los que está).",
        integration="slack",
    )
    def slack_list_channels() -> Any:
        items = channels()
        if not items:
            return "No veo canales."
        out = []
        for ch in items:
            item: Dict[str, Any] = {"id": ch["id"], "canal": "#" + ch.get("name", "")}
            if ch.get("is_private"):
                item["privado"] = True
            if ch.get("num_members"):
                item["miembros"] = ch["num_members"]
            if topic := (ch.get("purpose") or {}).get("value"):
                item["tema"] = topic[:100]
            out.append(item)
        return out

    @tool(
        "slack_send_message",
        "Envía un mensaje en Slack en nombre del usuario, a un canal (#canal) o "
        "por mensaje directo a una persona (@nombre).",
        {
            "destino": S(
                "«#canal», ID del canal, o «@persona» para un mensaje directo"
            ),
            "texto": S("Mensaje a enviar"),
        },
        ["destino", "texto"],
        writes=True,
        integration="slack",
    )
    def slack_send_message(destino: str, texto: str) -> str:
        dest = (destino or "").strip()
        if dest.startswith("@") or _USER_ID.match(dest):
            target = dest if _USER_ID.match(dest) else find_user(dest)
        else:
            target = channel_id(dest)
        res = _call(token, "chat.postMessage", post=True, channel=target, text=texto)
        return f"Mensaje enviado a {dest}." + (
            "" if not res.get("ts") else f" (hora {_when(res['ts'])})"
        )

    return [slack_search, slack_read_channel, slack_list_channels, slack_send_message]


register(
    Integration(
        id="slack",
        name="Slack",
        category="comunicacion",
        description="Busca y lee tus mensajes de Slack y escribe en tu nombre.",
        abilities=[
            "¿Qué se ha dicho hoy en #general?",
            "Busca en Slack lo que dijo Marta del presupuesto",
            "Escribe a Pedro por Slack que llego 10 minutos tarde",
        ],
        steps=[
            f'Pulsa este enlace: <a href="{MANIFEST_URL}" target="_blank" rel="noopener"><b>crear la app «Jarvis» en Slack</b></a> (inicia sesión en Slack si te lo pide). Ya va todo preconfigurado.',
            "Elige tu espacio de trabajo («workspace») y pulsa «<b>Next</b>», luego otra vez «<b>Next</b>» y por último «<b>Create</b>».",
            "En la página de la app, pulsa «<b>Install to Workspace</b>» (o ve a «OAuth & Permissions» en el menú de la izquierda) y luego «<b>Allow</b>» (Permitir).",
            "En «<b>OAuth & Permissions</b>» copia el «<b>User OAuth Token</b>» (empieza por <b>xoxp-</b>) y pégalo aquí abajo. ¡Listo!",
            "Nota: si tu empresa no deja instalar apps, Slack enviará una petición a tu administrador; cuando la apruebe, vuelve al paso 4.",
        ],
        fields=[Field("token", "User OAuth Token", placeholder="xoxp-…")],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas slack_*: buscar mensajes, leer un canal, listar canales y enviar mensajes como el usuario.",
        icon="#",
        color="#4A154B",
        link="https://slack.com",
    )
)
