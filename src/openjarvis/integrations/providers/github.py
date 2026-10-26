# ruff: noqa: E501  (user-facing guide texts are long prose)
"""GitHub through a personal access token (fine-grained or classic)."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Dict, List

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

API = "https://api.github.com"
AUTH_ERROR = (
    "GitHub no acepta el token, o no tiene permiso para esto. Revisa que no "
    "haya caducado y que tenga acceso a ese repositorio."
)
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_TYPES = {
    "Issue": "issue",
    "PullRequest": "PR",
    "Release": "release",
    "Discussion": "discusión",
}
_REASONS = {
    "assign": "te lo han asignado",
    "author": "lo creaste tú",
    "comment": "nuevo comentario",
    "mention": "te han mencionado",
    "review_requested": "te piden revisión",
    "team_mention": "mencionan a tu equipo",
    "state_change": "cambio de estado",
    "subscribed": "sigues el repositorio",
    "ci_activity": "resultado de CI",
    "security_alert": "alerta de seguridad",
}


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fecha(value: str | None) -> str:
    if not value:
        return ""
    try:
        from openjarvis.personal.reminders import local_tz

        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(local_tz())
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return value[:16].replace("T", " ")


def _repo(value: str) -> str:
    value = (value or "").strip().removeprefix("https://github.com/").strip("/")
    if not _REPO.match(value):
        raise IntegrationError(
            "Indica el repositorio como «usuario/repositorio» (p. ej. «octocat/hello-world»)."
        )
    return value


def _item(it: Dict[str, Any]) -> Dict[str, Any]:
    repo = (it.get("repository_url") or "").split("/repos/")[-1]
    out: Dict[str, Any] = {
        "repo": repo,
        "numero": it.get("number"),
        "tipo": "PR" if it.get("pull_request") else "issue",
        "titulo": it.get("title", ""),
        "estado": "abierto" if it.get("state") == "open" else "cerrado",
        "actualizado": _fecha(it.get("updated_at")),
        "url": it.get("html_url", ""),
    }
    if it.get("comments"):
        out["comentarios"] = it["comments"]
    return out


def _check(creds: Dict[str, str]) -> str:
    me = request(
        "GET", f"{API}/user", headers=_headers(creds["token"]), auth_error=AUTH_ERROR
    )
    return (me or {}).get("login") or "GitHub"


def _tools(creds: Dict[str, str]):
    h = _headers(creds["token"])
    cache: Dict[str, str] = {}

    def call(method: str, path: str, **kwargs: Any) -> Any:
        return request(
            method, f"{API}{path}", headers=h, auth_error=AUTH_ERROR, **kwargs
        )

    def login() -> str:
        if "login" not in cache:
            cache["login"] = call("GET", "/user")["login"]
        return cache["login"]

    def search(q: str, n: int) -> List[Dict[str, Any]]:
        data = call(
            "GET", "/search/issues", params={"q": q, "per_page": n, "sort": "updated"}
        )
        return (data or {}).get("items", [])

    @tool(
        "github_notifications",
        "Muestra las notificaciones de GitHub sin leer.",
        {"limite": I("Máximo de notificaciones (1-50)")},
        integration="github",
    )
    def github_notifications(limite: int = 20) -> Any:
        if creds["token"].strip().startswith("github_pat_"):
            raise IntegrationError(
                "Con un token «fine-grained» (github_pat_) GitHub no deja ver las "
                "notificaciones. Si las quieres, reconecta GitHub con un token clásico "
                "(ghp_) siguiendo el primer paso de la guía."
            )
        items = call(
            "GET",
            "/notifications",
            params={"per_page": max(1, min(int(limite or 20), 50))},
        )
        if not items:
            return "No tienes notificaciones sin leer en GitHub. 🎉"
        return [
            {
                "repo": (n.get("repository") or {}).get("full_name", ""),
                "tipo": _TYPES.get(
                    (n.get("subject") or {}).get("type"),
                    (n.get("subject") or {}).get("type", ""),
                ),
                "titulo": (n.get("subject") or {}).get("title", ""),
                "motivo": _REASONS.get(n.get("reason"), n.get("reason", "")),
                "fecha": _fecha(n.get("updated_at")),
            }
            for n in items
        ]

    @tool(
        "github_search",
        "Busca issues y pull requests en GitHub (admite la sintaxis de búsqueda de "
        "GitHub, p. ej. «repo:usuario/proyecto is:open error de login»).",
        {
            "consulta": S("Qué buscar (texto y/o filtros de GitHub)"),
            "tipo": S("«issue», «pr» o vacío para ambos"),
            "limite": I("Máximo de resultados (1-30)"),
        },
        ["consulta"],
        integration="github",
    )
    def github_search(consulta: str, tipo: str = "", limite: int = 10) -> Any:
        n = max(1, min(int(limite or 10), 30))
        q = consulta.strip()
        if re.search(r"\b(is|type):(issue|pr|pull-request)\b", q):
            queries = [q]
        elif (tipo or "").lower() in ("pr", "prs", "pull request"):
            queries = [f"{q} is:pr"]
        elif (tipo or "").lower() in ("issue", "issues"):
            queries = [f"{q} is:issue"]
        else:  # GitHub requires choosing issues or PRs in each search
            queries = [f"{q} is:issue", f"{q} is:pr"]
        items: List[Dict[str, Any]] = []
        for query in queries:
            items += search(query, n)
        items.sort(key=lambda it: it.get("updated_at") or "", reverse=True)
        if not items:
            return "No he encontrado nada con esa búsqueda."
        return [_item(it) for it in items[:n]]

    @tool(
        "github_my_items",
        "Lista mis issues y pull requests abiertos: PRs que he creado o que me piden "
        "revisar, e issues que he creado o que me han asignado.",
        integration="github",
    )
    def github_my_items() -> Any:
        me = login()
        groups = [
            ("mis PRs", f"is:open is:pr author:{me} archived:false"),
            (
                "PRs que me piden revisar",
                f"is:open is:pr review-requested:{me} archived:false",
            ),
            ("issues asignados a mí", f"is:open is:issue assignee:{me} archived:false"),
            ("issues que he creado", f"is:open is:issue author:{me} archived:false"),
        ]
        out: Dict[str, Any] = {}
        seen = set()
        for label, q in groups:
            rows = []
            for it in search(q, 20):
                if it.get("html_url") in seen:
                    continue
                seen.add(it.get("html_url"))
                row = _item(it)
                row.pop("estado", None)
                rows.append(row)
            if rows:
                out[label] = rows
        return out or "No tienes issues ni pull requests abiertos. 🎉"

    @tool(
        "github_read_issue",
        "Lee un issue o pull request de GitHub con sus últimos comentarios.",
        {
            "repo": S("Repositorio «usuario/repositorio»"),
            "numero": I("Número del issue o PR"),
        },
        ["repo", "numero"],
        integration="github",
    )
    def github_read_issue(repo: str, numero: int) -> str:
        path = f"/repos/{_repo(repo)}/issues/{int(numero)}"
        it = call("GET", path)
        kind = "PR" if it.get("pull_request") else "Issue"
        state = "abierto" if it.get("state") == "open" else "cerrado"
        labels = ", ".join(lb.get("name", "") for lb in it.get("labels") or [])
        head = [
            f"{kind} #{it.get('number')}: {it.get('title', '')} ({state})",
            f"Autor: {(it.get('user') or {}).get('login', '')} · {_fecha(it.get('created_at'))}",
        ]
        if labels:
            head.append(f"Etiquetas: {labels}")
        if assignees := ", ".join(
            a.get("login", "") for a in it.get("assignees") or []
        ):
            head.append(f"Asignado a: {assignees}")
        head.append(it.get("html_url", ""))
        body = (it.get("body") or "(sin descripción)").strip()[:2500]
        text = "\n".join(head) + "\n\n" + body
        total = int(it.get("comments") or 0)
        if total:
            per = 10
            comments = (
                call(
                    "GET",
                    f"{path}/comments",
                    params={"per_page": per, "page": max(1, math.ceil(total / per))},
                )
                or []
            )
            text += f"\n\nÚltimos comentarios ({len(comments)} de {total}):"
            for c in comments[-5:]:
                who = (c.get("user") or {}).get("login", "")
                text += f"\n— {who} ({_fecha(c.get('created_at'))}): {(c.get('body') or '').strip()[:500]}"
        return text

    @tool(
        "github_create_issue",
        "Crea un issue nuevo en un repositorio de GitHub.",
        {
            "repo": S("Repositorio «usuario/repositorio»"),
            "titulo": S("Título del issue"),
            "descripcion": S("Descripción (opcional, admite Markdown)"),
        },
        ["repo", "titulo"],
        writes=True,
        integration="github",
    )
    def github_create_issue(repo: str, titulo: str, descripcion: str = "") -> str:
        body: Dict[str, Any] = {"title": titulo}
        if descripcion:
            body["body"] = descripcion
        it = call("POST", f"/repos/{_repo(repo)}/issues", json=body)
        return f"Issue #{it.get('number')} creado: {it.get('html_url', '')}"

    @tool(
        "github_comment",
        "Añade un comentario a un issue o pull request de GitHub.",
        {
            "repo": S("Repositorio «usuario/repositorio»"),
            "numero": I("Número del issue o PR"),
            "texto": S("Comentario (admite Markdown)"),
        },
        ["repo", "numero", "texto"],
        writes=True,
        integration="github",
    )
    def github_comment(repo: str, numero: int, texto: str) -> str:
        c = call(
            "POST",
            f"/repos/{_repo(repo)}/issues/{int(numero)}/comments",
            json={"body": texto},
        )
        return f"Comentario publicado: {(c or {}).get('html_url', '')}".strip()

    return [
        github_notifications,
        github_search,
        github_my_items,
        github_read_issue,
        github_create_issue,
        github_comment,
    ]


register(
    Integration(
        id="github",
        name="GitHub",
        category="tecnico",
        description="Revisa notificaciones, issues y pull requests de GitHub.",
        abilities=[
            "¿Tengo algo pendiente en GitHub?",
            "Léeme el issue 42 de miempresa/web",
            "Abre un issue en miempresa/web: el formulario de contacto no envía",
        ],
        steps=[
            'Pulsa <a href="https://github.com/settings/tokens/new?scopes=repo,notifications&description=Jarvis" target="_blank" rel="noopener"><b>este enlace</b></a> (inicia sesión en GitHub si te lo pide). Se abre la página para crear un token con el nombre «Jarvis» y los permisos «repo» y «notifications» ya marcados.',
            "En «<b>Expiration</b>» elige cuánto tiempo quieres que funcione (por ejemplo, 90 días o «No expiration»).",
            "Baja hasta el final y pulsa «<b>Generate token</b>».",
            "Copia el código que aparece (empieza por <b>ghp_</b>; solo se muestra una vez) y pégalo aquí abajo. ¡Listo!",
            'Alternativa: si prefieres un token «fine-grained» (github_pat_…) limitado a algunos repositorios, créalo <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">aquí</a> con permisos de «Issues» y «Pull requests». Con ese tipo de token GitHub no deja ver las notificaciones.',
        ],
        fields=[
            Field(
                "token", "Token de acceso personal", placeholder="github_pat_… o ghp_…"
            )
        ],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas github_*: notificaciones, buscar, mis issues/PRs abiertos, leer un issue, crear issue y comentar.",
        icon="GH",
        color="#24292F",
        link="https://github.com",
    )
)
