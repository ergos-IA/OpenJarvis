"""Create a user's Jarvis instance -- shared by the console and the
self-service checkout.  Where it runs (Railway service or Docker container)
depends on the backend, see :mod:`openjarvis.fasttrack.backends`.
"""

from __future__ import annotations

import os
import re
import secrets
import unicodedata
from typing import Any, Dict, Optional

from openjarvis.fasttrack.backends import SHARED_REF, get_backend
from openjarvis.fasttrack.models import PROVIDERS, engine_for, provider_for

USER_PREFIX = "jarvis-"
CREDENTIALS = [p["credential"] for p in PROVIDERS.values()]
LEGAL_VARS = [
    "LEGAL_COMPANY",
    "LEGAL_NIF",
    "LEGAL_ADDRESS",
    "LEGAL_REGISTRY",
    "LEGAL_EMAIL",
    "LEGAL_DPO",
    "LEGAL_JURISDICTION",
    "LEGAL_HOSTING",
]
# No 0/O, 1/I/L: easy to read aloud and type.  12 chars ~ 60 bits.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:30].strip("-") or "usuario"


def new_api_key() -> str:
    return "oj_sk_" + secrets.token_urlsafe(32)


def new_access_code() -> str:
    """Short personal code for the public site (jarvis.aifasttrack.tech)."""
    chars = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return "-".join(chars[i : i + 4] for i in range(0, 12, 4))


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def model_vars(model: str, provider: str = "") -> Dict[str, str]:
    model = model.strip()
    if not model:
        raise ValueError("Elige un modelo")
    if provider and provider not in PROVIDERS:
        raise ValueError("Proveedor desconocido")
    provider = provider or provider_for(model)
    return {"JARVIS_MODEL": model, "JARVIS_ENGINE": engine_for(provider)}


def provision_user(
    rw: Any,
    *,
    name: str,
    email: str = "",
    model: str,
    provider: str = "",
    whatsapp: str = "personal",
    assistant_name: str = "Jarvis",
    extra_vars: Optional[Dict[str, str]] = None,
    shared_ai: bool = False,
    repo: str = "",
    branch: str = "",
) -> Dict[str, Any]:
    """Create the instance (variables + volume + domain) and start it.

    *rw* is a backend (or a Railway client, wrapped).

    ``shared_ai=False`` (the default) leaves the AI keys out: the user
    connects their own account in the setup wizard ("Conecta tu IA").
    ``True`` wires AI Fast Track's shared keys (AI included in the price).

    Returns ``{id, service, domain, link, access_code, build_error}``.
    Raises :class:`~openjarvis.fasttrack.errors.BackendError` (and
    ``ValueError`` for bad input).
    """
    name = " ".join(name.split())
    if not name:
        raise ValueError("Escribe el nombre del usuario")
    repo = repo or os.environ.get("JARVIS_REPO", "ergos-IA/OpenJarvis")
    branch = branch or os.environ.get("JARVIS_BRANCH", "main")

    backend = get_backend(rw)
    taken = {s["name"] for s in backend.services()}
    base = USER_PREFIX + slugify(name)
    service_name, n = base, 2
    while service_name in taken:
        service_name, n = f"{base}-{n}", n + 1

    key = new_api_key()
    access_code = new_access_code()
    variables = {
        "PORT": "8000",
        "OPENJARVIS_API_KEY": key,
        "JARVIS_ACCESS_CODE": access_code,
        "JARVIS_ASSISTANT_NAME": assistant_name.strip() or "Jarvis",
        "JARVIS_USER_NAME": name,
        "JARVIS_USER_EMAIL": email.strip(),
        "WHATSAPP_MODE": "self",
        "WHATSAPP_PROVIDER": "meta" if whatsapp == "meta" else "personal",
        **model_vars(model, provider),
        **(extra_vars or {}),
    }
    # Provider credentials and the company's legal data come from the
    # shared variables, filled in once for every user (Railway: the
    # project's shared variables; Docker: the shared env file).
    # Internal token the console/billing use to add credits to an instance.
    variables.setdefault("FASTTRACK_ADMIN_TOKEN", SHARED_REF % "FASTTRACK_ADMIN_TOKEN")
    for var in (CREDENTIALS if shared_ai else []) + LEGAL_VARS:
        variables.setdefault(var, SHARED_REF % var)

    created = backend.create_instance(service_name, variables, repo=repo, branch=branch)
    ends = backend.endpoints(created["id"], key, host=created["domain"])
    return {
        "id": created["id"],
        "service": service_name,
        "domain": created["domain"],
        "link": ends["link"],
        "access_code": access_code,
        "build_error": created["build_error"],
    }


__all__ = [
    "CREDENTIALS",
    "LEGAL_VARS",
    "USER_PREFIX",
    "model_vars",
    "new_access_code",
    "new_api_key",
    "normalize_code",
    "provision_user",
    "slugify",
]
