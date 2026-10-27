"""Building blocks for the external tools a user connects to their Jarvis.

An :class:`Integration` describes one external service: how the user
connects it (fields to paste, or an OAuth button), a short step-by-step
guide, how to check the credentials, and the tools the WhatsApp agent gets
while it is connected.  Providers live in :mod:`openjarvis.integrations.providers`
and register themselves with :func:`register`.

Tools are plain functions wrapped by :func:`tool`; they receive the saved
credentials and return text for the model.  Writes to external services
(create, send, update) are marked ``writes=True``: their description tells the
agent to confirm with the user first.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 6000
CONFIRM_NOTE = (
    " Modifica datos del usuario en un servicio externo: úsala solo si el "
    "usuario lo ha pedido y sigue su preferencia de confirmaciones."
)

CATEGORIES = {
    "productividad": "Productividad",
    "comunicacion": "Comunicación",
    "negocio": "Negocio y ventas",
    "tecnico": "Técnico",
    "personal": "Vida personal",
}


class IntegrationError(Exception):
    """A user-facing problem (bad key, service down...).  Message in Spanish."""


@dataclass
class Field:
    key: str
    label: str
    kind: str = "password"  # password | text | url | textarea
    placeholder: str = ""
    help: str = ""
    optional: bool = False


@dataclass
class OAuthSpec:
    """OAuth 2.0 authorization-code flow through the central relay."""

    provider: str  # google | strava | spotify | dropbox ...
    authorize_url: str
    token_url: str
    scopes: List[str]
    client_env: str  # e.g. "GOOGLE" -> OPENJARVIS_GOOGLE_CLIENT_ID/_SECRET
    extra_params: Dict[str, str] = field(default_factory=dict)
    scope_separator: str = " "
    basic_auth: bool = False  # client credentials in an Authorization header


@dataclass
class Integration:
    id: str
    name: str
    category: str
    description: str
    # What Jarvis can do with it, as short phrases for the UI.
    abilities: List[str]
    steps: List[str]  # step-by-step guide, may contain simple <a> / <b> HTML
    fields: List[Field] = field(default_factory=list)
    oauth: Optional[OAuthSpec] = None
    # check(creds) -> account label shown in the UI ("Workspace Acme").
    check: Optional[Callable[[Dict[str, str]], str]] = None
    build_tools: Optional[Callable[[Dict[str, str]], List[BaseTool]]] = None
    # Extra context for the agent's prompt while connected.
    prompt: str = ""
    always_on: bool = False  # needs no connection (e.g. weather)
    icon: str = ""  # 1-2 chars or emoji for the card
    color: str = ""  # brand colour for the card accent
    link: str = ""  # the service's website

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "category_label": CATEGORIES.get(self.category, self.category),
            "description": self.description,
            "abilities": self.abilities,
            "steps": self.steps,
            "fields": [f.__dict__ for f in self.fields],
            "oauth": bool(self.oauth),
            "always_on": self.always_on,
            "icon": self.icon,
            "color": self.color,
            "link": self.link,
        }


_REGISTRY: Dict[str, Integration] = {}


def register(integration: Integration) -> Integration:
    _REGISTRY[integration.id] = integration
    return integration


def registry() -> Dict[str, Integration]:
    from openjarvis.integrations import providers  # noqa: F401  (registers)

    return _REGISTRY


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class FunctionTool(BaseTool):
    """A tool backed by a Python function ``fn(**params) -> str | data``."""

    is_local = False

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        fn: Callable[..., Any],
        *,
        integration: str = "",
        writes: bool = False,
    ) -> None:
        self.tool_id = name
        self._fn = fn
        self._integration = integration
        self._spec = ToolSpec(
            name=name,
            description=description + (CONFIRM_NOTE if writes else ""),
            parameters=parameters,
            category="integration",
            timeout_seconds=45.0,
            metadata={"integration": integration, "writes": writes},
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def execute(self, **params: Any) -> ToolResult:
        try:
            out = self._fn(**params)
        except IntegrationError as exc:
            return ToolResult(self._spec.name, str(exc), success=False)
        except httpx.HTTPError as exc:
            logger.warning("%s: %s", self._spec.name, exc)
            return ToolResult(
                self._spec.name,
                f"No he podido contactar con el servicio: {exc}",
                success=False,
            )
        except TypeError as exc:  # bad/missing arguments from the model
            return ToolResult(self._spec.name, f"Parámetros no válidos: {exc}", False)
        except Exception as exc:  # never break the conversation
            logger.exception("Integration tool %s failed", self._spec.name)
            return ToolResult(self._spec.name, f"Error: {exc}", success=False)
        return ToolResult(self._spec.name, render(out))


def tool(
    name: str,
    description: str,
    properties: Optional[Dict[str, Any]] = None,
    required: Optional[List[str]] = None,
    *,
    writes: bool = False,
    integration: str = "",
) -> Callable[[Callable[..., Any]], FunctionTool]:
    """Decorator: turn a function into a :class:`FunctionTool`."""

    def wrap(fn: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            name,
            description,
            {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
            fn,
            integration=integration,
            writes=writes,
        )

    return wrap


def render(out: Any) -> str:
    if out is None:
        return "Hecho."
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n… (resultado recortado)"
    return text


def S(desc: str, **extra: Any) -> Dict[str, Any]:
    """JSON-schema string property."""
    return {"type": "string", "description": desc, **extra}


def I(desc: str, **extra: Any) -> Dict[str, Any]:  # noqa: E743
    """JSON-schema integer property."""
    return {"type": "integer", "description": desc, **extra}


def B(desc: str) -> Dict[str, Any]:
    return {"type": "boolean", "description": desc}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_http: Optional[httpx.Client] = None


def http() -> httpx.Client:
    """Shared client (tests replace it with :func:`set_http`)."""
    global _http
    if _http is None:
        _http = httpx.Client(
            timeout=httpx.Timeout(25.0, connect=10.0),
            headers={"User-Agent": "Jarvis-AIFastTrack/1.0"},
        )
    return _http


def set_http(client: Optional[httpx.Client]) -> None:
    global _http
    _http = client


def request(
    method: str,
    url: str,
    *,
    ok_status: tuple = (200, 201, 202, 204),
    auth_error: str = "La clave no es válida o ha caducado. Vuelve a conectarla.",
    **kwargs: Any,
) -> Any:
    """Call an API; return parsed JSON (or text), raise :class:`IntegrationError`."""
    resp = http().request(method, url, **kwargs)
    if resp.status_code == 401:
        raise IntegrationError(auth_error)
    if resp.status_code == 403:
        # 403 is not always a bad key: Google uses it for rate limits too.
        body = resp.text[:2000]
        if "ateLimit" in body or "quota" in body.lower():
            raise IntegrationError("El servicio pide esperar un poco (límite de uso).")
        raise IntegrationError(
            "No tengo permiso para esa operación. Si la clave es antigua o "
            "desmarcaste algún permiso al conectarlo, vuelve a conectarlo."
        )
    if resp.status_code == 404:
        raise IntegrationError("No encontrado (404).")
    if resp.status_code == 429:
        raise IntegrationError("El servicio pide esperar un poco (límite de uso).")
    if resp.status_code not in ok_status:
        detail = resp.text[:300]
        raise IntegrationError(f"El servicio respondió {resp.status_code}: {detail}")
    if resp.status_code == 204 or not resp.content:
        return None
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        return resp.json()
    return resp.text


def fetch_public(url: str, *, max_bytes: int = 5_000_000) -> str:
    """GET a URL the user pasted, refusing private/internal addresses."""
    from openjarvis.security.ssrf import check_ssrf

    for _ in range(4):  # follow a few redirects, checking every hop
        if not url.lower().startswith(("https://", "http://")):
            raise IntegrationError("El enlace debe empezar por https://")
        problem = check_ssrf(url)
        if problem:
            raise IntegrationError(f"Enlace no permitido: {problem}")
        resp = http().get(url, follow_redirects=False)
        if resp.is_redirect and resp.headers.get("location"):
            url = str(resp.url.join(resp.headers["location"]))
            continue
        if resp.status_code in (401, 403, 404):
            raise IntegrationError(
                "No se puede leer ese enlace (¿es el enlace secreto/público?)."
            )
        if resp.status_code != 200:
            raise IntegrationError(f"El enlace respondió {resp.status_code}.")
        if len(resp.content) > max_bytes:
            raise IntegrationError("El contenido del enlace es demasiado grande.")
        return resp.text
    raise IntegrationError("Demasiadas redirecciones.")


def pick(d: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Keep only some keys (drops empty values) — compact results for the model."""
    return {k: d[k] for k in keys if d.get(k) not in (None, "", [], {})}


__all__ = [
    "B",
    "CATEGORIES",
    "CONFIRM_NOTE",
    "Field",
    "FunctionTool",
    "I",
    "Integration",
    "IntegrationError",
    "OAuthSpec",
    "S",
    "fetch_public",
    "http",
    "pick",
    "register",
    "registry",
    "render",
    "request",
    "set_http",
    "tool",
]
