# ruff: noqa: E501  (user-facing guide texts are long prose)
"""Dropbox (read-only): search, browse, read text files, share a download link."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from openjarvis.integrations.base import (
    B,
    I,
    Integration,
    IntegrationError,
    OAuthSpec,
    S,
    http,
    pick,
    register,
    tool,
)

API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
AUTH_ERROR = (
    "Dropbox no me deja entrar: el acceso ha caducado o se retiró el permiso. "
    "Vuelve a conectar Dropbox desde la página de conectores."
)
MAX_TEXT = 5000
MAX_DOWNLOAD = 2_000_000
TEXT_EXT = {
    "txt", "md", "markdown", "csv", "tsv", "json", "xml", "html", "htm", "log",
    "ini", "cfg", "conf", "yaml", "yml", "py", "js", "ts", "css", "sql", "srt",
    "vtt", "tex", "rtf",
}  # fmt: skip


def _fail(resp: Any) -> None:
    if resp.status_code == 401:
        raise IntegrationError(AUTH_ERROR)
    if resp.status_code == 403:
        raise IntegrationError("Dropbox no da permiso para eso.")
    if resp.status_code == 409:
        summary = ""
        try:
            summary = resp.json().get("error_summary", "")
        except Exception:
            pass
        if "not_found" in summary:
            raise IntegrationError("No encuentro ese archivo o carpeta en Dropbox.")
        if "not_file" in summary or "not_folder" in summary:
            raise IntegrationError("Eso no es del tipo esperado (¿archivo o carpeta?).")
        raise IntegrationError(f"Dropbox no ha podido hacerlo ({summary or 'error'}).")
    if resp.status_code == 429:
        raise IntegrationError("Dropbox pide esperar un poco (límite de uso).")
    raise IntegrationError(f"Dropbox respondió {resp.status_code}: {resp.text[:200]}")


def _rpc(creds: Any, endpoint: str, body: Optional[Dict[str, Any]] = None) -> Any:
    headers = {"Authorization": f"Bearer {creds.token()}"}
    if body is None:
        resp = http().post(f"{API}/{endpoint}", headers=headers)
    else:
        resp = http().post(f"{API}/{endpoint}", headers=headers, json=body)
    if resp.status_code != 200:
        _fail(resp)
    return resp.json() if resp.content else {}


def _path(text: str) -> str:
    p = (text or "").strip()
    if p in ("", "/"):
        return ""
    if p.startswith(("id:", "rev:", "ns:")):
        return p
    return p if p.startswith("/") else "/" + p


def _size(n: Any) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _entry(m: Dict[str, Any]) -> Dict[str, Any]:
    folder = m.get(".tag") == "folder"
    return pick(
        {
            "nombre": m.get("name", ""),
            "ruta": m.get("path_display", ""),
            "tipo": "carpeta" if folder else "archivo",
            "tamaño": "" if folder else _size(m.get("size")),
            "modificado": (m.get("server_modified") or "")[:10],
        },
        "nombre",
        "ruta",
        "tipo",
        "tamaño",
        "modificado",
    )


def _check(creds: Any) -> str:
    me = _rpc(creds, "users/get_current_account")
    return me.get("email") or (me.get("name") or {}).get("display_name") or "Dropbox"


def _tools(creds: Any):
    @tool(
        "dropbox_search",
        "Busca archivos y carpetas en el Dropbox del usuario por nombre o contenido.",
        {
            "texto": S("Qué buscar"),
            "solo_nombre": B(
                "Buscar solo en el nombre (por defecto también dentro de los documentos)"
            ),
            "carpeta": S("Limitar a una carpeta, p. ej. /Facturas (opcional)"),
            "limite": I("Máximo de resultados (1-25, por defecto 10)"),
        },
        ["texto"],
        integration="dropbox",
    )
    def dropbox_search(
        texto: str, solo_nombre: bool = False, carpeta: str = "", limite: int = 10
    ) -> Any:
        options: Dict[str, Any] = {
            "max_results": max(1, min(int(limite or 10), 25)),
            "file_status": "active",
            "filename_only": bool(solo_nombre),
        }
        if carpeta:
            options["path"] = _path(carpeta)
        data = _rpc(creds, "files/search_v2", {"query": texto, "options": options})
        found = [
            ((m.get("metadata") or {}).get("metadata") or {})
            for m in data.get("matches", [])
        ]
        found = [m for m in found if m]
        if not found:
            return f"No encuentro nada con «{texto}» en Dropbox."
        return [_entry(m) for m in found]

    @tool(
        "dropbox_list_folder",
        "Lista lo que hay en una carpeta de Dropbox (vacío = carpeta principal).",
        {"carpeta": S("Ruta de la carpeta, p. ej. /Fotos/2024 (vacío = principal)")},
        integration="dropbox",
    )
    def dropbox_list_folder(carpeta: str = "") -> Any:
        data = _rpc(creds, "files/list_folder", {"path": _path(carpeta), "limit": 100})
        entries = data.get("entries", [])
        if not entries:
            return "La carpeta está vacía."
        entries.sort(
            key=lambda m: (m.get(".tag") != "folder", m.get("name", "").lower())
        )
        out: Any = [_entry(m) for m in entries]
        if data.get("has_more"):
            out.append({"nota": "Hay más archivos; usa dropbox_search para afinar."})
        return out

    @tool(
        "dropbox_read",
        "Lee el contenido de un archivo de texto de Dropbox (txt, md, csv, json...). "
        "Para otros tipos (PDF, fotos, Word...) dice qué es y cuánto ocupa.",
        {"ruta": S("Ruta del archivo (de dropbox_search o dropbox_list_folder)")},
        ["ruta"],
        integration="dropbox",
    )
    def dropbox_read(ruta: str) -> str:
        meta = _rpc(creds, "files/get_metadata", {"path": _path(ruta)})
        name = meta.get("name", ruta)
        if meta.get(".tag") == "folder":
            return f"«{name}» es una carpeta; usa dropbox_list_folder."
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        size = int(meta.get("size") or 0)
        if ext not in TEXT_EXT:
            return (
                f"«{name}» es un archivo {ext.upper() or 'sin extensión'} de "
                f"{_size(size)}; no puedo leer su texto, pero puedo darte un "
                "enlace de descarga (dropbox_link)."
            )
        if size > MAX_DOWNLOAD:
            return (
                f"«{name}» ocupa {_size(size)}: es demasiado grande para leerlo aquí."
            )
        resp = http().post(
            f"{CONTENT}/files/download",
            headers={
                "Authorization": f"Bearer {creds.token()}",
                # Header must be ASCII: json.dumps escapes accents as \\uXXXX.
                "Dropbox-API-Arg": json.dumps({"path": meta.get("id") or _path(ruta)}),
            },
        )
        if resp.status_code != 200:
            _fail(resp)
        text = resp.content.decode("utf-8", errors="replace").strip()
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n… (recortado)"
        return f"# {name}\n{text}" if text else f"«{name}» está vacío."

    @tool(
        "dropbox_link",
        "Da un enlace temporal (válido 4 horas) para descargar un archivo de Dropbox.",
        {"ruta": S("Ruta del archivo")},
        ["ruta"],
        integration="dropbox",
    )
    def dropbox_link(ruta: str) -> str:
        data = _rpc(creds, "files/get_temporary_link", {"path": _path(ruta)})
        name = (data.get("metadata") or {}).get("name", "el archivo")
        return f"Enlace para descargar «{name}» (caduca en 4 horas): {data.get('link', '')}"

    return [dropbox_search, dropbox_list_folder, dropbox_read, dropbox_link]


register(
    Integration(
        id="dropbox",
        name="Dropbox",
        category="productividad",
        description="Encuentra y lee tus archivos de Dropbox sin abrir el ordenador.",
        abilities=[
            "Busca en Dropbox la factura de la luz de marzo",
            "¿Qué hay en mi carpeta de Trabajo?",
            "Mándame el enlace del contrato de alquiler",
        ],
        steps=[
            "Pulsa el botón «<b>Conectar con Dropbox</b>».",
            "Entra con tu cuenta de Dropbox si te lo pide.",
            "Pulsa «<b>Permitir</b>». Jarvis solo podrá ver y leer tus archivos, nunca borrarlos ni cambiarlos.",
            "¡Listo! Volverás aquí y verás tu cuenta conectada.",
        ],
        oauth=OAuthSpec(
            provider="dropbox",
            authorize_url="https://www.dropbox.com/oauth2/authorize",
            token_url="https://api.dropboxapi.com/oauth2/token",
            scopes=["account_info.read", "files.metadata.read", "files.content.read"],
            client_env="DROPBOX",
            extra_params={"token_access_type": "offline"},
        ),
        check=_check,
        build_tools=_tools,
        prompt="Herramientas dropbox_*: buscar (dropbox_search), ver carpetas (dropbox_list_folder), leer archivos de texto (dropbox_read) y enlace de descarga (dropbox_link). Solo lectura.",
        icon="📦",
        color="#0061FF",
        link="https://www.dropbox.com",
    )
)
