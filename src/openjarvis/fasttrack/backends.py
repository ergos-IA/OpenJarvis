"""Where user instances run: Railway services or Docker containers.

The console (:mod:`admin`), the public site (:mod:`landing`), billing and
provisioning talk to a *backend* with one small interface, selected with
``JARVIS_BACKEND``:

* ``railway`` (default): one Railway service per user, its own
  ``*.up.railway.app`` domain (:class:`RailwayBackend`).
* ``docker``: one container ``jarvis-<slug>`` per user on a single VPS, all
  served at ``PUBLIC_URL`` and routed by Traefik with a ``jv=<slug>`` cookie
  (:class:`openjarvis.fasttrack.docker_backend.DockerBackend`).

Every backend lists *services* as dicts::

    {"id", "name", "created_at", "deploy_status", "volume_id", "aliases"}

``endpoints(id, key)`` says how to reach an instance::

    {"host":  public host name,
     "url":   base URL the control plane calls (internal on Docker),
     "link":  the user's personal link (carries the key),
     "route": value of the ``jv`` routing cookie ("" on Railway)}
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from openjarvis.fasttrack.railway import RailwayClient, RailwayError

SHARED_REF = "${{shared.%s}}"


class RailwayBackend:
    kind = "railway"
    label = "Railway"

    def __init__(self, rw: Any = None) -> None:
        self.rw = rw if rw is not None else RailwayClient()

    @property
    def configured(self) -> bool:
        return bool(getattr(self.rw, "configured", False))

    # -- reads ------------------------------------------------------------

    def services(self) -> List[Dict[str, Any]]:
        return self.rw.project_overview()["services"]

    def find(self, service_id: str) -> Optional[Dict[str, Any]]:
        for svc in self.services():
            if svc["id"] == service_id:
                return svc
        return None

    def resolve_id(self, service_id: str) -> str:
        return service_id

    def variables(self, service_id: str) -> Dict[str, str]:
        return self.rw.variables(service_id)

    def shared_variables(self) -> Dict[str, str]:
        return self.rw.variables()

    def endpoints(
        self, service_id: str, key: str = "", *, host: Optional[str] = None
    ) -> Dict[str, str]:
        domain = host if host is not None else self.rw.domain(service_id)
        return {
            "host": domain,
            "url": f"https://{domain}" if domain else "",
            "link": f"https://{domain}/#key={key}" if domain and key else "",
            "route": "",
        }

    def source(self, repo: str, branch: str) -> str:
        return f"{repo}@{branch}"

    # -- writes -----------------------------------------------------------

    def create_instance(
        self, name: str, variables: Dict[str, str], *, repo: str, branch: str
    ) -> Dict[str, str]:
        rw = self.rw
        variables = {
            "RAILWAY_DOCKERFILE_PATH": "deploy/railway/Dockerfile",
            **variables,
        }
        sid = rw.create_service(name)
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

    def set_variables(
        self, service_id: str, variables: Dict[str, str], *, deploy: bool = True
    ) -> None:
        self.rw.set_variables(service_id, variables, deploy=deploy)

    def redeploy(self, svc: Dict[str, Any], *, repo: str, branch: str) -> None:
        if svc.get("deploy_status"):
            self.rw.redeploy(svc["id"])
        else:
            # Never built (e.g. GitHub wasn't connected when it was created).
            self.rw.connect_repo(svc["id"], repo, branch)

    def delete(self, svc: Dict[str, Any]) -> None:
        self.rw.delete_service(svc["id"], svc.get("volume_id", ""))


def backend_kind() -> str:
    return (os.environ.get("JARVIS_BACKEND", "") or "railway").strip().lower()


def get_backend(obj: Any = None) -> Any:
    """A backend from *obj*: an existing backend is returned as is, a Railway
    client (or test double) is wrapped, ``None`` builds one from the env."""
    if obj is not None:
        if hasattr(obj, "endpoints") and hasattr(obj, "kind"):
            return obj
        return RailwayBackend(obj)
    if backend_kind() == "docker":
        from openjarvis.fasttrack.docker_backend import DockerBackend

        return DockerBackend()
    return RailwayBackend()


__all__ = ["RailwayBackend", "SHARED_REF", "backend_kind", "get_backend"]
