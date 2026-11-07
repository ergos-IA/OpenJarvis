"""Jarvis instances as Docker containers on one VPS (``JARVIS_BACKEND=docker``).

Talks to the Docker Engine HTTP API with ``httpx`` (no Docker SDK), normally
through a ``tecnativa/docker-socket-proxy`` sidecar
(``DOCKER_HOST=tcp://docker-proxy:2375``; ``unix:///...`` also works).

The containers are the source of truth -- there is no database:

* name ``jarvis-<slug>``, volume ``jarvis-<slug>-data`` mounted at ``/data``;
* labels ``fasttrack.jarvis=instance``, ``fasttrack.jarvis.slug=<slug>``,
  optionally ``fasttrack.jarvis.railway_id=<old Railway service id>`` (so
  Stripe metadata written on Railway still finds the instance) and
  ``fasttrack.jarvis.shared=A,B`` (variables copied from the shared env file);
* the environment holds the instance's variables, as on Railway.

The instance id used by the console and billing is the *slug*; the
container name and the old Railway id are accepted too.

All instances are served at ``PUBLIC_URL`` (one domain): Traefik routes a
request to ``jarvis-<slug>`` when it carries the cookie ``jv=<slug>``, which
the public site sets when the user enters their code.

Environment:

* ``DOCKER_HOST``           Docker API (default ``unix:///var/run/docker.sock``)
* ``JARVIS_IMAGE``          image of the instances (default ``openjarvis:latest``)
* ``JARVIS_SHARED_ENV``     shared variables file (``KEY=value`` lines),
  replaces Railway's ``${{shared.X}}`` (default ``/run/jarvis/shared.env``)
* ``JARVIS_DOCKER_NETWORK`` network shared with Traefik (default ``root_default``)
* ``JARVIS_TRAEFIK_ENTRYPOINT`` / ``JARVIS_TRAEFIK_CERTRESOLVER``
  (default ``websecure`` / ``mytlschallenge``)
* ``JARVIS_INSTANCE_MEMORY_MB`` optional memory limit per instance
* ``JARVIS_INSTANCE_SHARED``  shared variables copied into every instance
  (comma-separated; e.g. the OAuth and Microsoft app ids)
* ``PUBLIC_URL``            the single public site (default
  ``https://jarvis.aifasttrack.tech``)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from openjarvis.fasttrack.errors import BackendError

logger = logging.getLogger(__name__)

PREFIX = "jarvis-"
LABEL = "fasttrack.jarvis"
LABEL_SLUG = "fasttrack.jarvis.slug"
LABEL_RAILWAY = "fasttrack.jarvis.railway_id"
LABEL_SHARED = "fasttrack.jarvis.shared"
LABEL_ENV = "fasttrack.jarvis.env"
# ENV baked into deploy/railway/Dockerfile and its python base image; never
# carried over from an old container when recreating (see LABEL_ENV).
_IMAGE_ENV = {
    "PATH",
    "HOME",
    "LANG",
    "GPG_KEY",
    "PYTHON_VERSION",
    "PYTHON_SHA256",
    "OPENJARVIS_HOME",
    "OPENJARVIS_BAILEYS_BRIDGE_DIR",
    "OPENJARVIS_NO_UPDATE_CHECK",
    "PYTHONUNBUFFERED",
}
# Unversioned paths use the daemon's own API version (Docker 29 refuses
# versions below 1.44); pin one with DOCKER_API_VERSION=1.47 if needed.
API_VERSION = ""
INSTANCE_PORT = 8000
ROUTE_COOKIE = "jv"
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
_SHARED_RE = re.compile(r"\$\{\{\s*shared\.([A-Za-z0-9_]+)\s*\}\}")
_SECOND_NS = 1_000_000_000


class DockerError(BackendError):
    pass


def public_url() -> str:
    return os.environ.get("PUBLIC_URL", "https://jarvis.aifasttrack.tech").rstrip("/")


def public_host() -> str:
    return (urlparse(public_url()).hostname or "").lower()


def valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug or ""))


def container_name(slug: str) -> str:
    return PREFIX + slug


def volume_name(slug: str) -> str:
    return f"{PREFIX}{slug}-data"


# ---------------------------------------------------------------------------
# Shared variables (replaces Railway's ${{shared.X}})
# ---------------------------------------------------------------------------


def parse_env(text: str) -> Dict[str, str]:
    """``KEY=value`` lines (``#`` comments, optional ``export`` and quotes)."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def read_env_file(path: str) -> Dict[str, str]:
    try:
        return parse_env(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def substitute_shared(
    variables: Dict[str, str], shared: Dict[str, str]
) -> Tuple[Dict[str, str], List[str]]:
    """Replace ``${{shared.X}}`` with the shared value.

    Returns the concrete variables and the names of the variables that were
    (entirely) a shared reference, so a recreation can refresh them.
    """
    out: Dict[str, str] = {}
    refs: List[str] = []
    for key, value in variables.items():
        value = "" if value is None else str(value)
        whole = _SHARED_RE.fullmatch(value.strip())
        if whole:
            refs.append(key)
            if whole.group(1) != key:
                refs[-1] = f"{key}:{whole.group(1)}"
        out[key] = _SHARED_RE.sub(lambda m: shared.get(m.group(1), ""), value)
    return out, refs


def _parse_refs(label: str) -> Dict[str, str]:
    """``A,B:C`` -> {A: A, B: C} (variable -> shared name)."""
    refs: Dict[str, str] = {}
    for item in (label or "").split(","):
        item = item.strip()
        if not item:
            continue
        var, _, src = item.partition(":")
        refs[var] = src or var
    return refs


def instance_shared_vars() -> List[str]:
    """Shared variables every instance gets (``JARVIS_INSTANCE_SHARED``,
    comma-separated), e.g. ``MS_CLIENT_ID,OPENJARVIS_GOOGLE_CLIENT_ID``."""
    raw = os.environ.get("JARVIS_INSTANCE_SHARED", "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def _format_refs(refs: Dict[str, str]) -> str:
    return ",".join(
        var if src == var else f"{var}:{src}" for var, src in sorted(refs.items())
    )


# ---------------------------------------------------------------------------
# Traefik
# ---------------------------------------------------------------------------


def traefik_labels(
    slug: str,
    *,
    host: str,
    network: str,
    entrypoint: str = "websecure",
    certresolver: str = "mytlschallenge",
) -> Dict[str, str]:
    """Router ``jv-<slug>``: the public host + cookie ``jv=<slug>``.

    A second router serves ``/i/<slug>/...`` (prefix stripped) for callers
    that can't carry the cookie, e.g. webhooks from Meta's WhatsApp API.
    """
    r = f"jv-{slug}"
    rule = (
        f"Host(`{host}`) && "
        f"HeaderRegexp(`Cookie`, `(^|;\\s*){ROUTE_COOKIE}={slug}(;|$)`)"
    )
    labels = {
        "traefik.enable": "true",
        "traefik.docker.network": network,
        f"traefik.http.routers.{r}.rule": rule,
        f"traefik.http.routers.{r}.priority": "100",
        f"traefik.http.routers.{r}.entrypoints": entrypoint,
        f"traefik.http.routers.{r}.tls.certresolver": certresolver,
        f"traefik.http.routers.{r}.service": r,
        f"traefik.http.services.{r}.loadbalancer.server.port": str(INSTANCE_PORT),
        f"traefik.http.middlewares.{r}-strip.stripprefix.prefixes": f"/i/{slug}",
        f"traefik.http.routers.{r}-direct.rule": (
            f"Host(`{host}`) && PathPrefix(`/i/{slug}/`)"
        ),
        f"traefik.http.routers.{r}-direct.priority": "150",
        f"traefik.http.routers.{r}-direct.entrypoints": entrypoint,
        f"traefik.http.routers.{r}-direct.tls.certresolver": certresolver,
        f"traefik.http.routers.{r}-direct.middlewares": f"{r}-strip",
        f"traefik.http.routers.{r}-direct.service": r,
    }
    return labels


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def _http_for(docker_host: str, timeout: float) -> Tuple[httpx.Client, str]:
    if docker_host.startswith("unix://"):
        transport = httpx.HTTPTransport(uds=docker_host[len("unix://") :])
        return httpx.Client(transport=transport, timeout=timeout), "http://docker"
    if docker_host.startswith("tcp://"):
        docker_host = "http://" + docker_host[len("tcp://") :]
    return httpx.Client(timeout=timeout), docker_host.rstrip("/")


class DockerBackend:
    kind = "docker"
    label = "Docker"

    def __init__(
        self,
        docker_host: str = "",
        *,
        image: str = "",
        network: str = "",
        shared_env: str = "",
        http: Optional[httpx.Client] = None,
        base_url: str = "",
        stop_timeout_s: int = 20,
    ) -> None:
        self.docker_host = (
            docker_host
            or os.environ.get("DOCKER_HOST", "")
            or "unix:///var/run/docker.sock"
        )
        if http is not None:
            self._http, self._base = http, (base_url or "http://docker").rstrip("/")
        else:
            self._http, self._base = _http_for(self.docker_host, 60)
        self.image = image or os.environ.get("JARVIS_IMAGE", "") or "openjarvis:latest"
        self.network = (
            network or os.environ.get("JARVIS_DOCKER_NETWORK", "") or "root_default"
        )
        self.shared_env_path = (
            shared_env
            or os.environ.get("JARVIS_SHARED_ENV", "")
            or "/run/jarvis/shared.env"
        )
        self.entrypoint = os.environ.get("JARVIS_TRAEFIK_ENTRYPOINT", "websecure")
        self.certresolver = os.environ.get(
            "JARVIS_TRAEFIK_CERTRESOLVER", "mytlschallenge"
        )
        self.stop_timeout_s = stop_timeout_s

    configured = True

    # -- Docker API -------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Any = None,
        ok: Iterable[int] = (200, 201, 204),
    ) -> Any:
        version = os.environ.get("DOCKER_API_VERSION", API_VERSION).strip("/v ")
        prefix = f"/v{version}" if version else ""
        url = f"{self._base}{prefix}{path}"
        try:
            resp = self._http.request(method, url, params=params, json=body)
        except httpx.HTTPError as exc:
            raise DockerError(f"No se puede hablar con Docker: {exc}") from exc
        if resp.status_code not in tuple(ok):
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = resp.text[:200]
            raise DockerError(message or f"Docker respondió {resp.status_code}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _list(self) -> List[Dict[str, Any]]:
        return self._call(
            "GET",
            "/containers/json",
            params={
                "all": "1",
                "filters": json.dumps({"label": [f"{LABEL}=instance"]}),
            },
        )

    def inspect(self, slug: str) -> Optional[Dict[str, Any]]:
        try:
            return self._call("GET", f"/containers/{container_name(slug)}/json")
        except DockerError as exc:
            if "no such container" in str(exc).lower():
                return None
            raise

    def _volume_exists(self, name: str) -> bool:
        try:
            self._call("GET", f"/volumes/{name}")
            return True
        except DockerError as exc:
            if "no such volume" in str(exc).lower():
                return False
            raise

    # -- reads ------------------------------------------------------------

    @staticmethod
    def _status(state: str, status_text: str) -> str:
        """Container state -> the Railway-like status the console shows."""
        text = (status_text or "").lower()
        if state == "running":
            if "unhealthy" in text:
                return "UNHEALTHY"
            if "health: starting" in text:
                return "DEPLOYING"
            return "SUCCESS"
        if state == "restarting":
            return "DEPLOYING"
        if state == "created":
            return "INITIALIZING"
        if state in ("exited", "dead"):
            return "CRASHED"
        if state in ("paused", "removing"):
            return "REMOVED"
        return state.upper()

    def services(self) -> List[Dict[str, Any]]:
        out = []
        for c in self._list() or []:
            labels = c.get("Labels") or {}
            slug = labels.get(LABEL_SLUG, "")
            if not slug:
                continue
            created = c.get("Created") or 0
            out.append(
                {
                    "id": slug,
                    "name": container_name(slug),
                    "created_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)
                    ),
                    "deploy_status": self._status(
                        c.get("State", ""), c.get("Status", "")
                    ),
                    "volume_id": volume_name(slug),
                    "aliases": [a for a in [labels.get(LABEL_RAILWAY, "")] if a],
                }
            )
        return out

    def find(self, service_id: str) -> Optional[Dict[str, Any]]:
        service_id = (service_id or "").strip()
        if not service_id:
            return None
        for svc in self.services():
            if service_id in (svc["id"], svc["name"]) or service_id in svc["aliases"]:
                return svc
        return None

    def resolve_id(self, service_id: str) -> str:
        """Slug for a slug, container name or old Railway service id."""
        svc = self.find(service_id)
        if svc is None:
            raise DockerError(f"No existe la instancia {service_id}")
        return svc["id"]

    def variables(self, service_id: str) -> Dict[str, str]:
        data = self.inspect(service_id) if service_id else None
        if data is None:
            return {}
        out: Dict[str, str] = {}
        for item in (data.get("Config") or {}).get("Env") or []:
            key, _, value = item.partition("=")
            out[key] = value
        return out

    def shared_variables(self) -> Dict[str, str]:
        return read_env_file(self.shared_env_path)

    def endpoints(
        self, service_id: str, key: str = "", *, host: Optional[str] = None
    ) -> Dict[str, str]:
        return {
            "host": public_host(),
            "url": f"http://{container_name(service_id)}:{INSTANCE_PORT}",
            "link": f"{public_url()}/entrar#key={key}" if key else "",
            "route": service_id,
        }

    def source(self, repo: str, branch: str) -> str:
        return f"imagen {self.image}"

    # -- container lifecycle ----------------------------------------------

    def _labels(
        self,
        slug: str,
        *,
        railway_id: str = "",
        shared_refs: Dict[str, str],
        env_keys: Iterable[str] = (),
    ) -> Dict[str, str]:
        labels = {
            LABEL: "instance",
            LABEL_SLUG: slug,
            **traefik_labels(
                slug,
                host=public_host(),
                network=self.network,
                entrypoint=self.entrypoint,
                certresolver=self.certresolver,
            ),
        }
        if railway_id:
            labels[LABEL_RAILWAY] = railway_id
        if shared_refs:
            labels[LABEL_SHARED] = _format_refs(shared_refs)
        keys = sorted(env_keys)
        if keys:
            labels[LABEL_ENV] = ",".join(keys)
        return labels

    def _config(
        self, slug: str, env: Dict[str, str], labels: Dict[str, str]
    ) -> Dict[str, Any]:
        host_config: Dict[str, Any] = {
            "Mounts": [
                {"Type": "volume", "Source": volume_name(slug), "Target": "/data"}
            ],
            "RestartPolicy": {"Name": "unless-stopped"},
            "NetworkMode": self.network,
            "LogConfig": {
                "Type": "json-file",
                "Config": {"max-size": "10m", "max-file": "3"},
            },
        }
        memory = os.environ.get("JARVIS_INSTANCE_MEMORY_MB", "").strip()
        if memory.isdigit() and int(memory) > 0:
            host_config["Memory"] = int(memory) * 1024 * 1024
        return {
            "Image": self.image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Labels": labels,
            "ExposedPorts": {f"{INSTANCE_PORT}/tcp": {}},
            "StopTimeout": self.stop_timeout_s,
            "Healthcheck": {
                "Test": [
                    "CMD",
                    "python3",
                    "-c",
                    "import urllib.request; urllib.request.urlopen("
                    f"'http://127.0.0.1:{INSTANCE_PORT}/health', timeout=5)",
                ],
                "Interval": 30 * _SECOND_NS,
                "Timeout": 10 * _SECOND_NS,
                "StartPeriod": 180 * _SECOND_NS,
                # Checked every 5 s while starting: Traefik only routes to healthy
                # containers, so this shortens the wait after a (re)start.
                "StartInterval": 5 * _SECOND_NS,
                "Retries": 3,
            },
            "HostConfig": host_config,
            "NetworkingConfig": {"EndpointsConfig": {self.network: {}}},
        }

    def _instance_env(self, env: Dict[str, str]) -> Dict[str, str]:
        """Variables every Docker instance needs, whatever it was created with."""
        env = {k: v for k, v in env.items() if not k.startswith("RAILWAY_")}
        env["PORT"] = str(INSTANCE_PORT)
        env["PUBLIC_URL"] = public_url()
        env["JARVIS_ROUTING"] = "cookie"
        return env

    def _start_new(
        self, slug: str, env: Dict[str, str], labels: Dict[str, str]
    ) -> None:
        name = container_name(slug)
        self._call(
            "POST",
            "/containers/create",
            params={"name": name},
            body=self._config(slug, env, labels),
        )
        self._call("POST", f"/containers/{name}/start", ok=(204, 304))

    def _stop_and_remove(self, slug: str) -> None:
        name = container_name(slug)
        # Stop gracefully first: WhatsApp (Baileys) saves its session on exit.
        self._call(
            "POST",
            f"/containers/{name}/stop",
            params={"t": str(self.stop_timeout_s)},
            ok=(204, 304, 404),
        )
        self._call(
            "DELETE", f"/containers/{name}", params={"force": "1"}, ok=(204, 404)
        )

    def create_container(
        self,
        slug: str,
        variables: Dict[str, str],
        *,
        railway_id: str = "",
        reuse_volume: bool = False,
    ) -> Dict[str, str]:
        """Create and start ``jarvis-<slug>``.

        ``${{shared.X}}`` values are filled from the shared env file.  The
        volume is created if missing; an existing one is only accepted with
        ``reuse_volume`` (adopting an instance migrated from Railway).
        """
        if not valid_slug(slug):
            raise DockerError(f"Identificador no válido: {slug!r}")
        if self.inspect(slug) is not None:
            raise DockerError(f"Ya existe el contenedor {container_name(slug)}")
        if railway_id and self.find(railway_id) is not None:
            raise DockerError(f"El id de Railway {railway_id} ya está en uso")
        vol = volume_name(slug)
        if self._volume_exists(vol):
            if not reuse_volume:
                raise DockerError(
                    f"El volumen {vol} ya existe (¿datos de otra instancia?)"
                )
        else:
            self._call(
                "POST",
                "/volumes/create",
                body={"Name": vol, "Labels": {LABEL: "instance", LABEL_SLUG: slug}},
            )
        variables = dict(variables)
        for var in instance_shared_vars():
            variables.setdefault(var, "${{shared.%s}}" % var)
        env, refs = substitute_shared(variables, self.shared_variables())
        env = self._instance_env(env)
        labels = self._labels(
            slug,
            railway_id=railway_id,
            shared_refs=_parse_refs(",".join(refs)),
            env_keys=env,
        )
        self._start_new(slug, env, labels)
        return {"id": slug, "name": container_name(slug), "volume": vol}

    def recreate(
        self, service_id: str, updates: Optional[Dict[str, str]] = None
    ) -> None:
        """Replace the container with one from the current image, keeping its
        variables, labels and volume (plus *updates*).  Shared variables are
        refreshed from the shared env file."""
        slug = service_id
        old = self.inspect(slug) if valid_slug(slug) else None
        if old is None:
            slug = self.resolve_id(service_id)
            old = self.inspect(slug)
            if old is None:
                raise DockerError(f"No existe la instancia {service_id}")
        old_env: Dict[str, str] = {}
        for item in (old.get("Config") or {}).get("Env") or []:
            key, _, value = item.partition("=")
            old_env[key] = value
        old_labels = (old.get("Config") or {}).get("Labels") or {}
        # Only the instance's own variables: the image's ENV (PATH, HOME,
        # PYTHON_VERSION...) must come from the new image, not the old one.
        if old_labels.get(LABEL_ENV):
            own = set(old_labels[LABEL_ENV].split(","))
            old_env = {k: v for k, v in old_env.items() if k in own}
        else:
            image_env = self._image_env(old.get("Image", ""))
            old_env = {
                k: v
                for k, v in old_env.items()
                if k not in _IMAGE_ENV and image_env.get(k) != v
            }
        refs = _parse_refs(old_labels.get(LABEL_SHARED, ""))
        for var in instance_shared_vars():
            if var not in old_env and var not in refs:
                refs[var] = var  # newly listed in JARVIS_INSTANCE_SHARED
        updates = dict(updates or {})
        for key in updates:
            refs.pop(key, None)  # set explicitly: no longer follows shared
        shared = self.shared_variables()
        new_updates, new_refs = substitute_shared(updates, shared)
        refs.update(_parse_refs(",".join(new_refs)))
        env = {**old_env, **new_updates}
        for var, src in refs.items():
            if src in shared:
                env[var] = shared[src]
        env = self._instance_env(env)
        labels = self._labels(
            slug,
            railway_id=old_labels.get(LABEL_RAILWAY, ""),
            shared_refs=refs,
            env_keys=env,
        )
        self._stop_and_remove(slug)
        try:
            self._start_new(slug, env, labels)
        except DockerError:
            logger.exception("Recreating %s failed; restoring the old config", slug)
            try:
                self._call(
                    "DELETE",
                    f"/containers/{container_name(slug)}",
                    params={"force": "1"},
                    ok=(204, 404),
                )
                old_config = {
                    **self._config(slug, old_env, old_labels),
                    "Image": (old.get("Config") or {}).get("Image") or self.image,
                }
                self._call(
                    "POST",
                    "/containers/create",
                    params={"name": container_name(slug)},
                    body=old_config,
                )
                self._call(
                    "POST",
                    f"/containers/{container_name(slug)}/start",
                    ok=(204, 304),
                )
            except DockerError:
                logger.exception("Could not restore %s", slug)
            raise

    def _image_env(self, image_id: str) -> Dict[str, str]:
        if not image_id:
            return {}
        try:
            data = self._call("GET", f"/images/{image_id}/json")
        except DockerError:
            return {}
        out: Dict[str, str] = {}
        for item in ((data or {}).get("Config") or {}).get("Env") or []:
            key, _, value = item.partition("=")
            out[key] = value
        return out

    def remove(self, service_id: str, *, keep_volume: bool = False) -> None:
        slug = self.resolve_id(service_id)
        self._stop_and_remove(slug)
        if not keep_volume:
            self._call("DELETE", f"/volumes/{volume_name(slug)}", ok=(204, 404))

    # -- backend interface (console / billing / provisioning) ---------------

    def create_instance(
        self, name: str, variables: Dict[str, str], *, repo: str = "", branch: str = ""
    ) -> Dict[str, str]:
        slug = name[len(PREFIX) :] if name.startswith(PREFIX) else name
        self.create_container(slug, variables)
        return {"id": slug, "domain": public_host(), "build_error": ""}

    def set_variables(
        self, service_id: str, variables: Dict[str, str], *, deploy: bool = True
    ) -> None:
        # A container's environment is fixed: changing it means recreating.
        self.recreate(service_id, variables)

    def redeploy(
        self, svc: Dict[str, Any], *, repo: str = "", branch: str = ""
    ) -> None:
        self.recreate(svc["id"])

    def delete(self, svc: Dict[str, Any]) -> None:
        self.remove(svc["id"])


__all__ = [
    "DockerBackend",
    "DockerError",
    "LABEL",
    "LABEL_RAILWAY",
    "LABEL_ENV",
    "LABEL_SHARED",
    "LABEL_SLUG",
    "ROUTE_COOKIE",
    "container_name",
    "parse_env",
    "public_host",
    "public_url",
    "read_env_file",
    "substitute_shared",
    "traefik_labels",
    "valid_slug",
    "volume_name",
]
