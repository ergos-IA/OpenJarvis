"""Which external tools this Jarvis has connected, and their credentials.

Credentials live in ``<home>/integrations.json`` (owner-only), on the
instance's own volume.  While an integration is connected its tools are added
to the channel agent (``system.tools``) and a line goes into the prompt.

OAuth providers (Google, Spotify...) go through one redirect URI on the public
site (``PUBLIC_URL/oauth/callback``), registered once per provider app.  The
``state`` names this instance's host plus a single-use nonce; the site bounces
the browser back to ``/oauth/done`` here, which checks the nonce and exchanges
the code.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from openjarvis.core.paths import get_config_dir
from openjarvis.integrations.base import (
    Integration,
    IntegrationError,
    OAuthSpec,
    http,
    registry,
)
from openjarvis.tools._stubs import BaseTool

logger = logging.getLogger(__name__)

STATE_TTL_S = 15 * 60


def public_url() -> str:
    return os.environ.get("PUBLIC_URL", "https://jarvis.aifasttrack.tech").rstrip("/")


def oauth_redirect_uri() -> str:
    return f"{public_url()}/oauth/callback"


def oauth_client(spec: OAuthSpec) -> Optional[tuple]:
    cid = os.environ.get(f"OPENJARVIS_{spec.client_env}_CLIENT_ID", "")
    secret = os.environ.get(f"OPENJARVIS_{spec.client_env}_CLIENT_SECRET", "")
    return (cid, secret) if cid and secret else None


def encode_state(host: str, nonce: str) -> str:
    raw = json.dumps({"h": host, "n": nonce}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_state(state: str) -> Dict[str, str]:
    try:
        pad = "=" * (-len(state) % 4)
        data = json.loads(base64.urlsafe_b64decode(state + pad))
        return {"h": str(data["h"]), "n": str(data["n"])}
    except Exception:
        raise IntegrationError("Respuesta de autorización no válida.")


class Creds(dict):
    """Saved values of one integration; ``token()`` refreshes OAuth tokens."""

    def __init__(
        self, values: Dict[str, Any], refresh: Optional[Callable[[], str]] = None
    ):
        super().__init__(values)
        self._refresh = refresh

    def token(self) -> str:
        if self._refresh is not None:
            return self._refresh()
        return str(self.get("token", ""))


class IntegrationManager:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path or get_config_dir() / "integrations.json")
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = self._load()
        self._pending: Dict[str, Dict[str, Any]] = {}  # nonce -> {id, ts}
        self._tools: Optional[List[BaseTool]] = None
        self._system: Any = None
        self._base_tools: List[BaseTool] = []

    # -- storage ------------------------------------------------------------

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self._path)

    # -- catalogue ------------------------------------------------------------

    def _get(self, integration_id: str) -> Integration:
        found = registry().get(integration_id)
        if found is None:
            raise IntegrationError("Conector desconocido.")
        return found

    def connected(self, integration_id: str) -> bool:
        return integration_id in self._data

    def catalog(self) -> List[Dict[str, Any]]:
        out = []
        for integ in registry().values():
            item = integ.public()
            saved = self._data.get(integ.id)
            item["connected"] = bool(saved) or integ.always_on
            item["account"] = (saved or {}).get("account", "")
            item["connected_at"] = (saved or {}).get("connected_at", 0)
            item["available"] = not integ.oauth or bool(oauth_client(integ.oauth))
            out.append(item)
        return out

    # -- connect / disconnect -------------------------------------------------

    def connect(self, integration_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        integ = self._get(integration_id)
        if integ.oauth:
            raise IntegrationError("Este conector se conecta con el botón de acceso.")
        creds: Dict[str, str] = {}
        for f in integ.fields:
            value = str(values.get(f.key, "") or "").strip()
            if not value and not f.optional:
                raise IntegrationError(f"Falta: {f.label}.")
            if len(value) > 4000:
                raise IntegrationError(f"{f.label}: demasiado largo.")
            if value:
                creds[f.key] = value
        account = integ.check(Creds(creds)) if integ.check else ""
        self._store(integ.id, creds, account or "")
        return self.status(integ.id)

    def _store(self, integration_id: str, creds: Dict[str, Any], account: str) -> None:
        with self._lock:
            self._data[integration_id] = {
                "creds": creds,
                "account": account,
                "connected_at": int(time.time()),
            }
            self._save()
            self._changed()

    def disconnect(self, integration_id: str) -> None:
        self._get(integration_id)
        with self._lock:
            if self._data.pop(integration_id, None) is not None:
                self._save()
            self._changed()

    def status(self, integration_id: str) -> Dict[str, Any]:
        return next(i for i in self.catalog() if i["id"] == integration_id)

    # -- OAuth ----------------------------------------------------------------

    def begin_oauth(self, integration_id: str, host: str) -> str:
        integ = self._get(integration_id)
        if not integ.oauth:
            raise IntegrationError("Este conector no usa acceso con botón.")
        client = oauth_client(integ.oauth)
        if client is None:
            raise IntegrationError(
                f"{integ.name} aún no está activado en tu Jarvis. "
                "Escríbenos a hola@aifasttrack.tech."
            )
        nonce = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._pending = {
                k: v for k, v in self._pending.items() if now - v["ts"] < STATE_TTL_S
            }
            self._pending[nonce] = {"id": integ.id, "ts": now}
        spec = integ.oauth
        params = {
            "client_id": client[0],
            "redirect_uri": oauth_redirect_uri(),
            "response_type": "code",
            "scope": spec.scope_separator.join(spec.scopes),
            "state": encode_state(host, nonce),
            **spec.extra_params,
        }
        return f"{spec.authorize_url}?{urlencode(params)}"

    def finish_oauth(self, code: str, state: str) -> str:
        nonce = decode_state(state)["n"]
        with self._lock:
            pending = self._pending.pop(nonce, None)
        if pending is None or time.time() - pending["ts"] > STATE_TTL_S:
            raise IntegrationError("La autorización ha caducado. Vuelve a intentarlo.")
        integ = self._get(pending["id"])
        tokens = self._token_request(
            integ.oauth, grant_type="authorization_code", code=code
        )
        creds = self._tokens_to_creds(tokens, {})
        account = ""
        if integ.check:
            try:
                account = integ.check(self._creds(integ.id, creds))
            except IntegrationError:
                raise
            except Exception:
                logger.warning("%s: account check failed", integ.id, exc_info=True)
        self._store(integ.id, creds, account)
        return integ.id

    def _token_request(self, spec: OAuthSpec, **params: str) -> Dict[str, Any]:
        client = oauth_client(spec)
        if client is None:
            raise IntegrationError("Conector no activado.")
        data = {"redirect_uri": oauth_redirect_uri(), **params}
        kwargs: Dict[str, Any] = {
            "data": data,
            "headers": {"Accept": "application/json"},
        }
        if spec.basic_auth:
            kwargs["auth"] = client
        else:
            data.update(client_id=client[0], client_secret=client[1])
        if params.get("grant_type") == "refresh_token":
            data.pop("redirect_uri", None)
        resp = http().post(spec.token_url, **kwargs)
        if resp.status_code != 200:
            logger.warning(
                "OAuth token error %s: %s", resp.status_code, resp.text[:300]
            )
            if "invalid_grant" in resp.text and params.get("refresh_token"):
                raise IntegrationError(
                    "El acceso ha caducado o se ha retirado. Vuelve a conectarlo "
                    "desde la sección Conectores de tu Jarvis."
                )
            raise IntegrationError(
                "El servicio no ha aceptado la autorización. Vuelve a conectarlo."
            )
        return resp.json()

    @staticmethod
    def _tokens_to_creds(tokens: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, Any]:
        creds = dict(old)
        creds["access_token"] = tokens["access_token"]
        if tokens.get("refresh_token"):
            creds["refresh_token"] = tokens["refresh_token"]
        expires = tokens.get("expires_in") or tokens.get("expires_at")
        if tokens.get("expires_at"):
            creds["expires_at"] = int(tokens["expires_at"])
        elif expires:
            creds["expires_at"] = int(time.time() + int(expires))
        for key in ("athlete", "account_id", "scope"):
            if key in tokens:
                creds[key] = (
                    tokens[key]
                    if isinstance(tokens[key], str)
                    else json.dumps(tokens[key])
                )
        return creds

    def _fresh_token(self, integration_id: str) -> str:
        integ = self._get(integration_id)
        with self._lock:
            saved = self._data.get(integration_id)
            if saved is None:
                raise IntegrationError(f"{integ.name} no está conectado.")
            creds = saved["creds"]
            if creds.get("expires_at") and time.time() > creds["expires_at"] - 60:
                if not creds.get("refresh_token"):
                    raise IntegrationError(
                        f"El acceso a {integ.name} ha caducado. Vuelve a conectarlo."
                    )
                tokens = self._token_request(
                    integ.oauth,
                    grant_type="refresh_token",
                    refresh_token=creds["refresh_token"],
                )
                saved["creds"] = creds = self._tokens_to_creds(tokens, creds)
                self._save()
            return creds["access_token"]

    def _creds(self, integration_id: str, values: Dict[str, Any]) -> Creds:
        integ = registry()[integration_id]
        if integ.oauth:
            if integration_id in self._data:
                return Creds(values, lambda: self._fresh_token(integration_id))
            return Creds(values, lambda: values["access_token"])
        return Creds(values)

    # -- the agent -----------------------------------------------------------

    def tools(self) -> List[BaseTool]:
        with self._lock:
            if self._tools is None:
                tools: List[BaseTool] = []
                for integ in registry().values():
                    if not integ.build_tools:
                        continue
                    if not (integ.always_on or integ.id in self._data):
                        continue
                    creds = (self._data.get(integ.id) or {}).get("creds", {})
                    try:
                        tools.extend(integ.build_tools(self._creds(integ.id, creds)))
                    except Exception:
                        logger.exception("Tools for %s unavailable", integ.id)
                self._tools = tools
            return list(self._tools)

    def prompt_context(self) -> str:
        lines = []
        for integ in registry().values():
            saved = self._data.get(integ.id)
            if not (saved or integ.always_on) or not integ.build_tools:
                continue
            account = f" ({saved['account']})" if saved and saved.get("account") else ""
            lines.append(f"- {integ.name}{account}. {integ.prompt}".rstrip())
        if not lines:
            return ""
        return (
            "\n\nHerramientas externas conectadas (úsalas cuando el usuario lo "
            "pida; antes de crear, cambiar o enviar algo en ellas, confirma):\n"
            + "\n".join(lines)
        )

    def attach(self, system: Any) -> None:
        with self._lock:
            ours = {id(t) for t in (self._tools or [])}
            self._system = system
            self._base_tools = [t for t in (system.tools or []) if id(t) not in ours]
            self._apply()

    def _changed(self) -> None:
        self._tools = None
        self._apply()

    def _apply(self) -> None:
        if self._system is None:
            return
        taken = {getattr(t.spec, "name", "") for t in self._base_tools}
        extra = [t for t in self.tools() if t.spec.name not in taken]
        self._system.tools = self._base_tools + extra


_manager: Optional[IntegrationManager] = None
_manager_lock = threading.Lock()


def get_manager() -> IntegrationManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = IntegrationManager()
        return _manager


def reset_manager() -> None:
    global _manager
    with _manager_lock:
        _manager = None


__all__ = [
    "Creds",
    "IntegrationManager",
    "decode_state",
    "encode_state",
    "get_manager",
    "oauth_client",
    "oauth_redirect_uri",
    "public_url",
    "reset_manager",
]
