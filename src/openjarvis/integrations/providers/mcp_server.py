# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Advanced: any remote MCP server (Streamable HTTP) with an optional token."""

from __future__ import annotations

import logging
from typing import Dict, List

from openjarvis.integrations.base import Field, Integration, IntegrationError, register

logger = logging.getLogger(__name__)

_clients: Dict[str, object] = {}  # url -> live MCPClient (one per server)


def _connect(creds: Dict[str, str]):
    from openjarvis.mcp.client import MCPClient
    from openjarvis.mcp.transport import StreamableHTTPTransport
    from openjarvis.security.ssrf import check_ssrf

    url = creds.get("url", "").strip()
    if not url.startswith("https://"):
        raise IntegrationError(
            "La dirección del servidor MCP debe empezar por https://"
        )
    problem = check_ssrf(url)
    if problem:
        raise IntegrationError(f"Dirección no permitida: {problem}")
    old = _clients.pop(url, None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    client = MCPClient(
        StreamableHTTPTransport(url=url, token=creds.get("token") or None)
    )
    try:
        client.initialize()
    except Exception as exc:
        try:
            client.close()
        except Exception:
            pass
        raise IntegrationError(f"No he podido conectar con el servidor MCP: {exc}")
    _clients[url] = client
    return client


def _check(creds: Dict[str, str]) -> str:
    client = _connect(creds)
    tools = client.list_tools()
    if not tools:
        raise IntegrationError("El servidor MCP no ofrece ninguna herramienta.")
    return f"{creds.get('name') or 'Servidor MCP'} · {len(tools)} herramientas"


def _tools(creds: Dict[str, str]) -> List:
    from openjarvis.tools.mcp_adapter import MCPToolProvider

    client = _clients.get(creds.get("url", "")) or _connect(creds)
    return MCPToolProvider(client).discover()


register(
    Integration(
        id="mcp",
        name="Servidor MCP (avanzado)",
        category="tecnico",
        description="Conecta cualquier herramienta que ofrezca un servidor MCP remoto.",
        abilities=[
            "Las que ofrezca el servidor: CRMs, bases de datos, herramientas internas…"
        ],
        steps=[
            "MCP es el estándar abierto para conectar asistentes de IA con herramientas. Muchos servicios publican un servidor MCP remoto (búscalo en su documentación como «MCP server»).",
            "Pega la dirección del servidor (https://…/mcp) y, si la pide, la clave o token de acceso.",
            "Solo se admiten servidores con clave fija; los que requieren iniciar sesión con botón (OAuth) aún no.",
        ],
        fields=[
            Field(
                "name",
                "Nombre (para reconocerlo)",
                kind="text",
                placeholder="Mi CRM",
                optional=True,
            ),
            Field(
                "url",
                "Dirección del servidor MCP",
                kind="url",
                placeholder="https://ejemplo.com/mcp",
            ),
            Field("token", "Token de acceso", placeholder="(opcional)", optional=True),
        ],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas de un servidor MCP del usuario; confirma antes de cambiar datos.",
        icon="⌘",
        color="#a78bfa",
        link="https://modelcontextprotocol.io",
    )
)
