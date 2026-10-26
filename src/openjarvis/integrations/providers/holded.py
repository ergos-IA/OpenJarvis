# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Holded (Spanish invoicing / ERP) through an API key pasted by the user.

Read-heavy on purpose: invoices, contacts, products and a billing summary.
The only write is creating a contact; invoices are never sent from here.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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

API = "https://api.holded.com/api/invoicing/v1"
AUTH_ERROR = (
    "Holded no acepta esa API key. Revisa que la has copiado entera o crea una nueva."
)


def _tz():
    from openjarvis.personal.reminders import local_tz

    return local_tz()


def _headers(key: str) -> Dict[str, str]:
    return {"key": key, "Accept": "application/json"}


def eur(value: Any) -> str:
    """1234.5 -> «1.234,50 €» (Spanish format)."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    text = f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def _day(ts: Any) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, _tz()).strftime("%d/%m/%Y")


def _parse_day(text: str, label: str) -> Optional[date]:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        raise IntegrationError(f"Fecha «{label}» no válida: usa AAAA-MM-DD.")


def _ts(d: date, end: bool = False) -> int:
    dt = datetime(d.year, d.month, d.day, tzinfo=_tz())
    if end:
        dt += timedelta(days=1, seconds=-1)
    return int(dt.timestamp())


def _pending(doc: Dict[str, Any]) -> float:
    if doc.get("paymentsPending") is not None:
        try:
            return float(doc["paymentsPending"])
        except (TypeError, ValueError):
            pass
    try:
        return float(doc.get("total") or 0) - float(doc.get("paymentsTotal") or 0)
    except (TypeError, ValueError):
        return 0.0


def _overdue(doc: Dict[str, Any], now: float) -> bool:
    due = doc.get("dueDate")
    try:
        return _pending(doc) > 0.005 and bool(due) and int(due) < now
    except (TypeError, ValueError):
        return False


def _documents(h: Dict[str, str], start: date, end: date) -> List[Dict[str, Any]]:
    data = request(
        "GET",
        f"{API}/documents/invoice",
        headers=h,
        params={
            "starttmp": _ts(start),
            "endtmp": _ts(end, end=True),
            "sort": "created-desc",
        },
        auth_error=AUTH_ERROR,
    )
    return data if isinstance(data, list) else []


def _invoice_line(doc: Dict[str, Any], now: float) -> Dict[str, Any]:
    pending = _pending(doc)
    if pending <= 0.005:
        estado = "cobrada"
    elif _overdue(doc, now):
        estado = "vencida"
    elif float(doc.get("paymentsTotal") or 0) > 0:
        estado = "cobrada en parte"
    else:
        estado = "pendiente de cobro"
    out = {
        "id": doc.get("id"),
        "numero": doc.get("docNumber"),
        "cliente": doc.get("contactName"),
        "fecha": _day(doc.get("date")),
        "vence": _day(doc.get("dueDate")),
        "total": eur(doc.get("total")),
        "estado": estado,
        "pendiente": eur(pending) if pending > 0.005 else "",
        "concepto": (doc.get("desc") or "")[:120],
    }
    return pick(out, *out.keys())


def _period(periodo: str) -> Tuple[date, date, str]:
    """(first day, last day, label) for «mes», «mes pasado», «año», AAAA-MM, AAAA."""
    today = datetime.now(_tz()).date()
    p = (periodo or "mes").strip().lower()
    if p in ("mes", "este mes", "month"):
        return today.replace(day=1), today, f"de {today:%m/%Y}"
    if p in ("año", "ano", "este año", "year"):
        return today.replace(month=1, day=1), today, f"de {today.year}"
    if p in ("mes pasado", "mes anterior"):
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last, f"de {last:%m/%Y}"
    try:
        if len(p) == 7 and p[4] == "-":  # AAAA-MM
            start = date(int(p[:4]), int(p[5:]), 1)
            nxt = (start + timedelta(days=32)).replace(day=1)
            return start, nxt - timedelta(days=1), f"de {start:%m/%Y}"
        if len(p) == 4 and p.isdigit():
            return date(int(p), 1, 1), date(int(p), 12, 31), f"de {p}"
    except ValueError:
        pass
    raise IntegrationError("Periodo no válido: usa «mes», «año», AAAA-MM o AAAA.")


def _check(creds: Dict[str, str]) -> str:
    today = datetime.now(_tz()).date()
    _documents(_headers(creds["token"]), today, today)
    return "Cuenta de Holded"


def _tools(creds: Dict[str, str]):
    h = _headers(creds["token"])

    @tool(
        "holded_list_invoices",
        "Lista las facturas de venta de Holded. Se pueden filtrar por estado "
        "(pendientes de cobro, vencidas o todas) y por fechas de emisión.",
        {
            "estado": S(
                "«pendientes» (sin cobrar del todo), «vencidas» (sin cobrar y "
                "pasada la fecha de vencimiento) o «todas» (por defecto)",
                enum=["todas", "pendientes", "vencidas"],
            ),
            "desde": S(
                "Fecha de emisión inicial AAAA-MM-DD (por defecto, hace 90 días; 1 año para pendientes/vencidas)"
            ),
            "hasta": S("Fecha de emisión final AAAA-MM-DD (por defecto hoy)"),
            "cliente": S("Filtrar por nombre de cliente (opcional)"),
            "limite": I("Máximo de facturas (1-50, por defecto 20)"),
        },
        integration="holded",
    )
    def holded_list_invoices(
        estado: str = "todas",
        desde: str = "",
        hasta: str = "",
        cliente: str = "",
        limite: int = 20,
    ) -> Any:
        estado = (estado or "todas").lower()
        today = datetime.now(_tz()).date()
        end = _parse_day(hasta, "hasta") or today
        default_days = 90 if estado == "todas" else 365
        start = _parse_day(desde, "desde") or end - timedelta(days=default_days)
        docs = _documents(h, start, end)
        now = time.time()
        if estado.startswith("pend"):
            docs = [d for d in docs if _pending(d) > 0.005]
        elif estado.startswith("venc"):
            docs = [d for d in docs if _overdue(d, now)]
        if cliente:
            q = cliente.lower()
            docs = [d for d in docs if q in (d.get("contactName") or "").lower()]
        if not docs:
            return f"No hay facturas {'' if estado == 'todas' else estado + ' '}entre el {start:%d/%m/%Y} y el {end:%d/%m/%Y}."
        limit = max(1, min(int(limite or 20), 50))
        lines = [_invoice_line(d, now) for d in docs[:limit]]
        total_pend = sum(_pending(d) for d in docs)
        head = f"{len(docs)} factura(s) entre el {start:%d/%m/%Y} y el {end:%d/%m/%Y}"
        if total_pend > 0.005:
            head += f"; pendiente de cobro en total: {eur(total_pend)}"
        return {"resumen": head, "facturas": lines}

    @tool(
        "holded_get_invoice",
        "Muestra el detalle de una factura de Holded: cliente, fechas, líneas, "
        "importes y lo que queda por cobrar.",
        {"id": S("ID de la factura (de holded_list_invoices)")},
        ["id"],
        integration="holded",
    )
    def holded_get_invoice(id: str) -> Any:
        doc = request(
            "GET",
            f"{API}/documents/invoice/{id}",
            headers=h,
            auth_error=AUTH_ERROR,
        )
        if not isinstance(doc, dict) or not doc:
            raise IntegrationError("No encuentro esa factura.")
        out = _invoice_line(doc, time.time())
        out.update(
            pick(
                {
                    "base_imponible": eur(doc.get("subtotal"))
                    if doc.get("subtotal") is not None
                    else "",
                    "impuestos": eur(doc.get("tax"))
                    if doc.get("tax") is not None
                    else "",
                    "cobrado": eur(doc.get("paymentsTotal"))
                    if doc.get("paymentsTotal")
                    else "",
                    "notas": (doc.get("notes") or "")[:300],
                },
                "base_imponible",
                "impuestos",
                "cobrado",
                "notas",
            )
        )
        lines = []
        for p in (doc.get("products") or [])[:30]:
            units = p.get("units") or 1
            lines.append(
                f"{units} × {p.get('name') or p.get('desc') or '(sin nombre)'} a {eur(p.get('price'))}"
                + (f" (IVA {p.get('tax')}%)" if p.get("tax") not in (None, "") else "")
            )
        if lines:
            out["lineas"] = lines
        return out

    @tool(
        "holded_search_contacts",
        "Busca clientes y proveedores en Holded por nombre, NIF, email o teléfono.",
        {
            "texto": S("Qué buscar (nombre, NIF/CIF, email o teléfono)"),
            "limite": I("Máximo de resultados (1-30, por defecto 10)"),
        },
        ["texto"],
        integration="holded",
    )
    def holded_search_contacts(texto: str, limite: int = 10) -> Any:
        data = request("GET", f"{API}/contacts", headers=h, auth_error=AUTH_ERROR)
        contacts = data if isinstance(data, list) else []
        q = (texto or "").lower().strip()
        found = [
            c
            for c in contacts
            if not q
            or q
            in " ".join(
                str(c.get(k) or "")
                for k in ("name", "tradeName", "code", "email", "phone", "mobile")
            ).lower()
        ]
        if not found:
            return f"No encuentro ningún contacto con «{texto}»."
        return [
            pick(
                {
                    "id": c.get("id"),
                    "nombre": c.get("name"),
                    "nombre_comercial": c.get("tradeName"),
                    "nif": c.get("code"),
                    "email": c.get("email"),
                    "telefono": c.get("mobile") or c.get("phone"),
                    "tipo": c.get("type"),
                },
                "id",
                "nombre",
                "nombre_comercial",
                "nif",
                "email",
                "telefono",
                "tipo",
            )
            for c in found[: max(1, min(int(limite or 10), 30))]
        ]

    @tool(
        "holded_create_contact",
        "Crea un contacto (cliente o proveedor) nuevo en Holded.",
        {
            "nombre": S("Nombre o razón social"),
            "nif": S("NIF/CIF (opcional)"),
            "email": S("Email (opcional)"),
            "telefono": S("Teléfono (opcional)"),
            "tipo": S(
                "«client» (cliente, por defecto), «supplier» (proveedor) o «lead» (posible cliente)",
                enum=["client", "supplier", "lead"],
            ),
            "direccion": S("Dirección (opcional)"),
            "ciudad": S("Ciudad (opcional)"),
            "codigo_postal": S("Código postal (opcional)"),
        },
        ["nombre"],
        writes=True,
        integration="holded",
    )
    def holded_create_contact(
        nombre: str,
        nif: str = "",
        email: str = "",
        telefono: str = "",
        tipo: str = "client",
        direccion: str = "",
        ciudad: str = "",
        codigo_postal: str = "",
    ) -> str:
        if not (nombre or "").strip():
            raise IntegrationError("Falta el nombre del contacto.")
        body: Dict[str, Any] = {"name": nombre.strip(), "type": tipo or "client"}
        if nif:
            body["code"] = nif.strip()
        if email:
            body["email"] = email.strip()
        if telefono:
            body["mobile"] = telefono.strip()
        address = pick(
            {
                "address": direccion,
                "city": ciudad,
                "postalCode": codigo_postal,
                "country": "España" if (direccion or ciudad or codigo_postal) else "",
            },
            "address",
            "city",
            "postalCode",
            "country",
        )
        if address:
            body["billAddress"] = address
        res = request(
            "POST", f"{API}/contacts", headers=h, json=body, auth_error=AUTH_ERROR
        )
        cid = (res or {}).get("id", "") if isinstance(res, dict) else ""
        return f"Contacto «{nombre.strip()}» creado en Holded." + (
            f" (id {cid})" if cid else ""
        )

    @tool(
        "holded_list_products",
        "Lista los productos y servicios del catálogo de Holded con su precio y stock.",
        {
            "texto": S("Filtrar por nombre o referencia (opcional)"),
            "limite": I("Máximo de resultados (1-50, por defecto 20)"),
        },
        integration="holded",
    )
    def holded_list_products(texto: str = "", limite: int = 20) -> Any:
        data = request("GET", f"{API}/products", headers=h, auth_error=AUTH_ERROR)
        items = data if isinstance(data, list) else []
        if texto:
            q = texto.lower()
            items = [
                p
                for p in items
                if q
                in f"{p.get('name') or ''} {p.get('sku') or ''} {p.get('desc') or ''}".lower()
            ]
        if not items:
            return "No hay productos que coincidan."
        out = []
        for p in items[: max(1, min(int(limite or 20), 50))]:
            row = {
                "id": p.get("id"),
                "nombre": p.get("name"),
                "referencia": p.get("sku"),
                "precio": eur(p.get("price")) if p.get("price") is not None else "",
                "iva": f"{p['tax']}%" if p.get("tax") not in (None, "") else "",
                "stock": p.get("stock") if p.get("kind") != "service" else "",
            }
            out.append(pick(row, *row.keys()))
        return out

    @tool(
        "holded_summary",
        "Resumen de facturación de Holded: total facturado, cobrado, pendiente de "
        "cobro y vencido en un periodo (este mes por defecto, o el año).",
        {
            "periodo": S(
                "«mes» (este mes, por defecto), «mes pasado», «año» (este año), un mes AAAA-MM o un año AAAA"
            ),
        },
        integration="holded",
    )
    def holded_summary(periodo: str = "mes") -> str:
        start, end, label = _period(periodo)
        docs = _documents(h, start, end)
        now = time.time()
        total = sum(float(d.get("total") or 0) for d in docs)
        base = sum(float(d.get("subtotal") or 0) for d in docs)
        pending = sum(max(_pending(d), 0.0) for d in docs)
        overdue_docs = [d for d in docs if _overdue(d, now)]
        overdue = sum(_pending(d) for d in overdue_docs)
        lines = [
            f"Facturación {label} ({start:%d/%m/%Y} – {end:%d/%m/%Y}, importes con IVA):",
            f"- Facturas emitidas: {len(docs)}",
            f"- Total facturado: {eur(total)} (base imponible {eur(base)})",
            f"- Cobrado: {eur(total - pending)}",
            f"- Pendiente de cobro: {eur(pending)}",
        ]
        if overdue_docs:
            lines.append(
                f"- De eso, vencido: {eur(overdue)} en {len(overdue_docs)} factura(s)"
            )
        return "\n".join(lines)

    return [
        holded_list_invoices,
        holded_get_invoice,
        holded_search_contacts,
        holded_create_contact,
        holded_list_products,
        holded_summary,
    ]


register(
    Integration(
        id="holded",
        name="Holded",
        category="negocio",
        description="Consulta tus facturas, cobros pendientes, clientes y productos.",
        abilities=[
            "¿Qué facturas tengo pendientes de cobro?",
            "¿Cuánto he facturado este mes?",
            "Busca el teléfono del cliente Construcciones Pérez",
        ],
        steps=[
            'Entra en <a href="https://app.holded.com" target="_blank" rel="noopener">app.holded.com</a> con tu usuario (hace falta ser administrador de la cuenta).',
            "Pulsa en tu nombre o el engranaje → <b>Configuración</b> → <b>Más</b> → <b>Desarrolladores</b>.",
            "Pulsa «<b>Nueva API key</b>», ponle de nombre «Jarvis» y guárdala.",
            "Copia la clave que aparece (una serie larga de letras y números) y pégala aquí.",
            "Jarvis solo consultará tus datos y, si se lo pides, creará contactos. Nunca enviará facturas.",
        ],
        fields=[Field("token", "API key de Holded", placeholder="a1b2c3d4…")],
        check=_check,
        build_tools=_tools,
        prompt="Herramientas holded_*: facturas (lista, detalle, resumen de lo facturado y pendiente), contactos (buscar, crear) y productos.",
        icon="H",
        color="#ff5a5f",
        link="https://www.holded.com",
    )
)
