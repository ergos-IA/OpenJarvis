# ruff: noqa: E501  (user-facing guide texts are long prose)
"""HubSpot CRM through a static token (service key or legacy private app)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from openjarvis.integrations.base import (
    Field,
    I,
    Integration,
    IntegrationError,
    S,
    pick,
    register,
    request,
    tool,
)

API = "https://api.hubapi.com"
AUTH_ERROR = (
    "HubSpot no acepta esa clave o le faltan permisos. Revisa que has copiado "
    "la clave entera y que has marcado los permisos de la guía."
)
# HUBSPOT_DEFINED association types (v4 docs): deal → contact, note → contact.
DEAL_TO_CONTACT = 3
NOTE_TO_CONTACT = 202

CONTACT_PROPS = ["firstname", "lastname", "email", "phone", "company", "jobtitle"]
COMPANY_PROPS = ["name", "domain", "city", "phone", "industry"]
DEAL_PROPS = ["dealname", "amount", "dealstage", "pipeline", "closedate"]


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def eur(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    text = f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def _day(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return iso[:10]


def _limit(n: Any, default: int = 10, top: int = 50) -> int:
    try:
        return max(1, min(int(n or default), top))
    except (TypeError, ValueError):
        return default


def _check(creds: Dict[str, str]) -> str:
    info = request(
        "GET",
        f"{API}/account-info/v3/details",
        headers=_headers(creds["token"]),
        auth_error=AUTH_ERROR,
    )
    portal = (info or {}).get("portalId")
    return f"Cuenta HubSpot {portal}" if portal else "HubSpot"


def _tools(creds: Dict[str, str]):
    h = _headers(creds["token"])
    stages_cache: Dict[str, str] = {}
    stage_pipeline: Dict[str, str] = {}

    def stages() -> Dict[str, str]:
        """dealstage id -> label (best effort; empty if not allowed)."""
        if not stages_cache:
            try:
                data = request(
                    "GET",
                    f"{API}/crm/v3/pipelines/deals",
                    headers=h,
                    auth_error=AUTH_ERROR,
                )
            except IntegrationError:
                return {}
            for pipe in (data or {}).get("results", []):
                for st in pipe.get("stages", []):
                    stages_cache[st["id"]] = st.get("label") or st["id"]
                    stage_pipeline.setdefault(st["id"], pipe.get("id") or "default")
        return stages_cache

    def search(obj: str, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = request(
            "POST",
            f"{API}/crm/v3/objects/{obj}/search",
            headers=h,
            json=body,
            auth_error=AUTH_ERROR,
        )
        return (data or {}).get("results", [])

    @tool(
        "hubspot_search_contacts",
        "Busca contactos en el CRM de HubSpot por nombre, email, teléfono o empresa.",
        {
            "texto": S("Qué buscar (vacío = los últimos modificados)"),
            "limite": I("Máximo de resultados (1-50, por defecto 10)"),
        },
        integration="hubspot",
    )
    def hubspot_search_contacts(texto: str = "", limite: int = 10) -> Any:
        body: Dict[str, Any] = {
            "limit": _limit(limite),
            "properties": CONTACT_PROPS,
            "sorts": [{"propertyName": "lastmodifieddate", "direction": "DESCENDING"}],
        }
        if texto:
            body["query"] = texto
        found = search("contacts", body)
        if not found:
            return "No he encontrado contactos."
        out = []
        for r in found:
            p = r.get("properties") or {}
            row = {
                "id": r.get("id"),
                "nombre": " ".join(
                    x for x in (p.get("firstname"), p.get("lastname")) if x
                ),
                "email": p.get("email"),
                "telefono": p.get("phone"),
                "empresa": p.get("company"),
                "cargo": p.get("jobtitle"),
            }
            out.append(pick(row, *row.keys()))
        return out

    @tool(
        "hubspot_search_companies",
        "Busca empresas en el CRM de HubSpot por nombre o dominio web.",
        {
            "texto": S("Qué buscar (vacío = las últimas modificadas)"),
            "limite": I("Máximo de resultados (1-50, por defecto 10)"),
        },
        integration="hubspot",
    )
    def hubspot_search_companies(texto: str = "", limite: int = 10) -> Any:
        body: Dict[str, Any] = {
            "limit": _limit(limite),
            "properties": COMPANY_PROPS,
            "sorts": [
                {"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}
            ],
        }
        if texto:
            body["query"] = texto
        found = search("companies", body)
        if not found:
            return "No he encontrado empresas."
        out = []
        for r in found:
            p = r.get("properties") or {}
            row = {
                "id": r.get("id"),
                "nombre": p.get("name"),
                "web": p.get("domain"),
                "ciudad": p.get("city"),
                "telefono": p.get("phone"),
                "sector": p.get("industry"),
            }
            out.append(pick(row, *row.keys()))
        return out

    @tool(
        "hubspot_list_deals",
        "Lista o busca negocios (oportunidades de venta) en HubSpot, con su etapa, "
        "importe y fecha de cierre. Se puede filtrar por etapa.",
        {
            "texto": S("Buscar por nombre del negocio (opcional)"),
            "etapa": S(
                "Nombre de la etapa, p. ej. «Cita programada» o «Cerrado ganado» (opcional)"
            ),
            "limite": I("Máximo de resultados (1-50, por defecto 20)"),
        },
        integration="hubspot",
    )
    def hubspot_list_deals(texto: str = "", etapa: str = "", limite: int = 20) -> Any:
        labels = stages()
        body: Dict[str, Any] = {
            "limit": _limit(limite, 20),
            "properties": DEAL_PROPS,
            "sorts": [
                {"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}
            ],
        }
        if texto:
            body["query"] = texto
        if etapa:
            q = etapa.strip().lower()
            ids = [
                sid
                for sid, lab in labels.items()
                if q in lab.lower() or q == sid.lower()
            ]
            body["filterGroups"] = [
                {
                    "filters": [
                        {"propertyName": "dealstage", "operator": "IN", "values": ids}
                        if ids
                        else {
                            "propertyName": "dealstage",
                            "operator": "EQ",
                            "value": etapa.strip(),
                        }
                    ]
                }
            ]
        found = search("deals", body)
        if not found:
            return "No he encontrado negocios."
        out = []
        for r in found:
            p = r.get("properties") or {}
            row = {
                "id": r.get("id"),
                "negocio": p.get("dealname"),
                "importe": eur(p.get("amount")),
                "etapa": labels.get(p.get("dealstage") or "", p.get("dealstage")),
                "cierre": _day(p.get("closedate")),
            }
            out.append(pick(row, *row.keys()))
        return out

    @tool(
        "hubspot_create_contact",
        "Crea un contacto nuevo en HubSpot.",
        {
            "nombre": S("Nombre"),
            "apellidos": S("Apellidos (opcional)"),
            "email": S("Email (recomendado: HubSpot lo usa para no duplicar)"),
            "telefono": S("Teléfono (opcional)"),
            "empresa": S("Empresa (opcional)"),
        },
        ["nombre"],
        writes=True,
        integration="hubspot",
    )
    def hubspot_create_contact(
        nombre: str,
        apellidos: str = "",
        email: str = "",
        telefono: str = "",
        empresa: str = "",
    ) -> str:
        props = pick(
            {
                "firstname": nombre.strip(),
                "lastname": apellidos.strip(),
                "email": email.strip(),
                "phone": telefono.strip(),
                "company": empresa.strip(),
            },
            "firstname",
            "lastname",
            "email",
            "phone",
            "company",
        )
        if not props.get("firstname"):
            raise IntegrationError("Falta el nombre del contacto.")
        res = request(
            "POST",
            f"{API}/crm/v3/objects/contacts",
            headers=h,
            json={"properties": props},
            ok_status=(200, 201, 409),
            auth_error=AUTH_ERROR,
        )
        if (res or {}).get("category") == "CONFLICT" or (res or {}).get(
            "status"
        ) == "error":
            return f"Ya existe un contacto con el email {email} en HubSpot; no lo he duplicado."
        full = " ".join(x for x in (nombre.strip(), apellidos.strip()) if x)
        return f"Contacto «{full}» creado en HubSpot (id {(res or {}).get('id', '?')})."

    @tool(
        "hubspot_create_deal",
        "Crea un negocio (oportunidad de venta) en HubSpot, opcionalmente asociado a un contacto.",
        {
            "nombre": S("Nombre del negocio, p. ej. «Reforma cocina García»"),
            "importe": S("Importe en euros (opcional), p. ej. «1500»"),
            "etapa": S("Etapa (opcional; por defecto la primera del embudo)"),
            "fecha_cierre": S("Fecha prevista de cierre AAAA-MM-DD (opcional)"),
            "contacto_id": S(
                "ID del contacto a asociar (de hubspot_search_contacts, opcional)"
            ),
        },
        ["nombre"],
        writes=True,
        integration="hubspot",
    )
    def hubspot_create_deal(
        nombre: str,
        importe: str = "",
        etapa: str = "",
        fecha_cierre: str = "",
        contacto_id: str = "",
    ) -> str:
        props: Dict[str, Any] = {"dealname": nombre.strip(), "pipeline": "default"}
        if importe:
            amount = str(importe).replace("€", "").replace(" ", "")
            if "," in amount:
                amount = amount.replace(".", "").replace(",", ".")
            try:
                props["amount"] = str(float(amount))
            except ValueError:
                raise IntegrationError(
                    "Importe no válido: usa solo números, p. ej. 1500."
                )
        labels = stages()
        if etapa:
            q = etapa.strip().lower()
            match = next(
                (
                    sid
                    for sid, lab in labels.items()
                    if lab.lower() == q or sid.lower() == q
                ),
                None,
            ) or next((sid for sid, lab in labels.items() if q in lab.lower()), None)
            props["dealstage"] = match or etapa.strip()
        else:
            props["dealstage"] = next(iter(labels), "appointmentscheduled")
        props["pipeline"] = stage_pipeline.get(props["dealstage"], "default")
        if fecha_cierre:
            try:
                d = datetime.fromisoformat(fecha_cierre.strip()[:10]).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                raise IntegrationError("Fecha de cierre no válida: usa AAAA-MM-DD.")
            props["closedate"] = d.strftime("%Y-%m-%dT00:00:00Z")
        body: Dict[str, Any] = {"properties": props}
        if contacto_id:
            body["associations"] = [
                {
                    "to": {"id": str(contacto_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": DEAL_TO_CONTACT,
                        }
                    ],
                }
            ]
        res = request(
            "POST",
            f"{API}/crm/v3/objects/deals",
            headers=h,
            json=body,
            auth_error=AUTH_ERROR,
        )
        return (
            f"Negocio «{nombre}» creado en HubSpot (id {(res or {}).get('id', '?')})."
        )

    @tool(
        "hubspot_add_note",
        "Añade una nota a la ficha de un contacto de HubSpot (p. ej. el resumen de una llamada).",
        {
            "contacto_id": S("ID del contacto (de hubspot_search_contacts)"),
            "texto": S("Texto de la nota"),
        },
        ["contacto_id", "texto"],
        writes=True,
        integration="hubspot",
    )
    def hubspot_add_note(contacto_id: str, texto: str) -> str:
        if not (texto or "").strip():
            raise IntegrationError("La nota está vacía.")
        body = {
            "properties": {
                "hs_note_body": texto.strip()[:60000],
                "hs_timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            },
            "associations": [
                {
                    "to": {"id": str(contacto_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": NOTE_TO_CONTACT,
                        }
                    ],
                }
            ],
        }
        request(
            "POST",
            f"{API}/crm/v3/objects/notes",
            headers=h,
            json=body,
            auth_error=AUTH_ERROR,
        )
        return "Nota añadida a la ficha del contacto en HubSpot."

    return [
        hubspot_search_contacts,
        hubspot_search_companies,
        hubspot_list_deals,
        hubspot_create_contact,
        hubspot_create_deal,
        hubspot_add_note,
    ]


register(
    Integration(
        id="hubspot",
        name="HubSpot",
        category="negocio",
        description="Consulta y apunta clientes, empresas y ventas en tu CRM.",
        abilities=[
            "¿Qué negocios tengo en «Contrato enviado»?",
            "Apunta en la ficha de Laura que la llamé hoy y quiere presupuesto",
            "Crea el contacto Pedro Ruiz, pedro@ruiz.es",
        ],
        steps=[
            'Entra en <a href="https://app.hubspot.com" target="_blank" rel="noopener">app.hubspot.com</a> con un usuario <b>superadministrador</b>.',
            "En el menú de la izquierda abre <b>Desarrollo</b> (Development) → <b>Claves</b> (Keys) → <b>Claves de servicio</b> (Service keys) y pulsa «<b>Crear clave de servicio</b>». Ponle de nombre «Jarvis».",
            "Pulsa «<b>Añadir nuevo alcance</b>» y marca estos permisos: <b>crm.objects.contacts.read</b>, <b>crm.objects.contacts.write</b> (incluye las notas), <b>crm.objects.companies.read</b>, <b>crm.objects.deals.read</b> y <b>crm.objects.deals.write</b>. Pulsa «Actualizar» y luego «<b>Crear</b>».",
            "Abre la clave que acabas de crear, pulsa «<b>Mostrar</b>» y luego «<b>Copiar</b>». Empieza por <b>pat-</b>. Pégala aquí.",
            "¿No ves «Claves de servicio»? Usa una app privada: <b>Desarrollo</b> → <b>Apps heredadas</b> (Legacy apps) → «Crear app heredada» → <b>Privada</b>, marca los mismos permisos en la pestaña «Alcances», crea la app y copia su <b>token de acceso</b>.",
        ],
        fields=[Field("token", "Clave de HubSpot", placeholder="pat-eu1-…")],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas hubspot_*: buscar contactos, empresas y negocios; crear contactos y negocios; añadir notas a un contacto.",
        icon="🧲",
        color="#ff7a59",
        link="https://www.hubspot.es",
    )
)
