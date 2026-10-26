"""Minimal Railway GraphQL client for provisioning Jarvis instances.

Uses a workspace/account token (``RAILWAY_API_TOKEN``).  ``RAILWAY_PROJECT_ID``
and ``RAILWAY_ENVIRONMENT_ID`` are injected by Railway into every service, so
the console manages the project it runs in.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx

API_URL = "https://backboard.railway.com/graphql/v2"


class RailwayError(RuntimeError):
    pass


class RailwayClient:
    def __init__(
        self,
        token: str = "",
        project_id: str = "",
        environment_id: str = "",
        *,
        http: Optional[httpx.Client] = None,
    ) -> None:
        self.token = token or os.environ.get("RAILWAY_API_TOKEN", "")
        self.project_id = project_id or os.environ.get("RAILWAY_PROJECT_ID", "")
        self.environment_id = environment_id or os.environ.get(
            "RAILWAY_ENVIRONMENT_ID", ""
        )
        self._http = http or httpx.Client(timeout=30)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.project_id and self.environment_id)

    def gql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Any:
        if not self.configured:
            raise RailwayError("Falta RAILWAY_API_TOKEN (o el proyecto) en la consola")
        resp = self._http.post(
            API_URL,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            data = resp.json()
        except ValueError:
            raise RailwayError(f"Railway respondió {resp.status_code}") from None
        if data.get("errors"):
            raise RailwayError(data["errors"][0].get("message", "Error de Railway"))
        return data.get("data") or {}

    # -- reads ------------------------------------------------------------

    def project_overview(self) -> Dict[str, Any]:
        """Services, their volumes and latest deployment status."""
        data = self.gql(
            """
            query($p: String!) {
              project(id: $p) {
                services { edges { node { id name createdAt
                  deployments(first: 1) { edges { node { id status createdAt } } }
                } } }
                volumes { edges { node { id name
                  volumeInstances { edges { node { serviceId mountPath } } }
                } } }
              }
            }
            """,
            {"p": self.project_id},
        )
        project = data.get("project") or {}
        volumes: Dict[str, str] = {}
        for edge in (project.get("volumes") or {}).get("edges", []):
            node = edge["node"]
            for vi in node["volumeInstances"]["edges"]:
                volumes[vi["node"]["serviceId"]] = node["id"]
        services = []
        for edge in (project.get("services") or {}).get("edges", []):
            node = edge["node"]
            deps = node.get("deployments", {}).get("edges", [])
            services.append(
                {
                    "id": node["id"],
                    "name": node["name"],
                    "created_at": node.get("createdAt", ""),
                    "deploy_status": deps[0]["node"]["status"] if deps else "",
                    "volume_id": volumes.get(node["id"], ""),
                }
            )
        return {"services": services}

    def variables(self, service_id: str = "") -> Dict[str, str]:
        """Rendered variables of a service, or the shared ones."""
        args = {"p": self.project_id, "e": self.environment_id}
        if service_id:
            args["s"] = service_id
            query = (
                "query($p: String!, $e: String!, $s: String) "
                "{ variables(projectId: $p, environmentId: $e, serviceId: $s) }"
            )
        else:
            query = (
                "query($p: String!, $e: String!) "
                "{ variables(projectId: $p, environmentId: $e) }"
            )
        return self.gql(query, args).get("variables") or {}

    def domain(self, service_id: str) -> str:
        data = self.gql(
            """
            query($p: String!, $e: String!, $s: String!) {
              domains(projectId: $p, environmentId: $e, serviceId: $s) {
                serviceDomains { domain } customDomains { domain }
              }
            }
            """,
            {"p": self.project_id, "e": self.environment_id, "s": service_id},
        )
        domains = data.get("domains") or {}
        for kind in ("customDomains", "serviceDomains"):
            for item in domains.get(kind) or []:
                return item["domain"]
        return ""

    # -- writes -----------------------------------------------------------

    def create_service(self, name: str) -> str:
        data = self.gql(
            """
            mutation($i: ServiceCreateInput!) { serviceCreate(input: $i) { id } }
            """,
            {"i": {"projectId": self.project_id, "name": name}},
        )
        return data["serviceCreate"]["id"]

    def set_variables(
        self, service_id: str, variables: Dict[str, str], *, deploy: bool = False
    ) -> None:
        self.gql(
            """
            mutation($i: VariableCollectionUpsertInput!) {
              variableCollectionUpsert(input: $i)
            }
            """,
            {
                "i": {
                    "projectId": self.project_id,
                    "environmentId": self.environment_id,
                    "serviceId": service_id,
                    "variables": variables,
                    "skipDeploys": not deploy,
                }
            },
        )

    def create_volume(self, service_id: str, mount_path: str = "/data") -> str:
        data = self.gql(
            """
            mutation($i: VolumeCreateInput!) { volumeCreate(input: $i) { id } }
            """,
            {
                "i": {
                    "projectId": self.project_id,
                    "environmentId": self.environment_id,
                    "serviceId": service_id,
                    "mountPath": mount_path,
                }
            },
        )
        return data["volumeCreate"]["id"]

    def create_domain(self, service_id: str, port: int = 8000) -> str:
        data = self.gql(
            """
            mutation($i: ServiceDomainCreateInput!) {
              serviceDomainCreate(input: $i) { domain }
            }
            """,
            {
                "i": {
                    "serviceId": service_id,
                    "environmentId": self.environment_id,
                    "targetPort": port,
                }
            },
        )
        return data["serviceDomainCreate"]["domain"]

    def set_healthcheck(self, service_id: str, path: str = "/health") -> None:
        self.gql(
            """
            mutation($s: String!, $e: String!, $i: ServiceInstanceUpdateInput!) {
              serviceInstanceUpdate(serviceId: $s, environmentId: $e, input: $i)
            }
            """,
            {
                "s": service_id,
                "e": self.environment_id,
                "i": {"healthcheckPath": path, "healthcheckTimeout": 300},
            },
        )

    def connect_repo(self, service_id: str, repo: str, branch: str) -> None:
        """Point the service at a GitHub repo; Railway builds and deploys it."""
        self.gql(
            """
            mutation($id: String!, $i: ServiceConnectInput!) {
              serviceConnect(id: $id, input: $i) { id }
            }
            """,
            {"id": service_id, "i": {"repo": repo, "branch": branch}},
        )

    def redeploy(self, service_id: str) -> None:
        self.gql(
            """
            mutation($s: String!, $e: String!) {
              serviceInstanceDeployV2(serviceId: $s, environmentId: $e)
            }
            """,
            {"s": service_id, "e": self.environment_id},
        )

    def delete_service(self, service_id: str, volume_id: str = "") -> None:
        self.gql(
            """
            mutation($id: String!, $e: String!) {
              serviceDelete(id: $id, environmentId: $e)
            }
            """,
            {"id": service_id, "e": self.environment_id},
        )
        if volume_id:
            self.gql(
                "mutation($v: String!) { volumeDelete(volumeId: $v) }",
                {"v": volume_id},
            )


__all__ = ["RailwayClient", "RailwayError"]
