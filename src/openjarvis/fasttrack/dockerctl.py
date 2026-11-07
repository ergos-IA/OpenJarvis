"""Operate the Jarvis instances on the VPS (Docker backend).

Run it where the console runs (same ``DOCKER_HOST``, ``JARVIS_IMAGE``,
``JARVIS_SHARED_ENV`` and ``PUBLIC_URL``), e.g.::

    docker compose -f deploy/vps/docker-compose.yml exec console \
        python -m openjarvis.fasttrack.dockerctl list

Commands:

* ``list``                       instances, status and old Railway id
* ``create --env-file F --slug S [--railway-id ID]``
                                 create ``jarvis-S`` from a ``KEY=value``
                                 file (e.g. exported from Railway).  An
                                 existing volume ``jarvis-S-data`` is reused
                                 (instance migrated with its /data).
* ``recreate S`` / ``recreate --all``
                                 new container from the current image,
                                 same variables, labels and volume
* ``set S KEY=value [KEY=value ...]``
                                 change variables (recreates the container)
* ``delete S --confirm S [--keep-volume]``
* ``print-labels S``             the labels (Traefik included) of S
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from openjarvis.fasttrack.docker_backend import (
    LABEL_RAILWAY,
    DockerBackend,
    DockerError,
    container_name,
    read_env_file,
    valid_slug,
)


def _print_table(rows: List[List[str]]) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())


def cmd_list(b: DockerBackend, args: argparse.Namespace) -> int:
    services = sorted(b.services(), key=lambda s: s["id"])
    if not services:
        print("No hay instancias.")
        return 0
    rows = [["SLUG", "ESTADO", "USUARIO", "RAILWAY_ID", "CREADO"]]
    for svc in services:
        env = b.variables(svc["id"])
        user = env.get("JARVIS_USER_NAME", "")
        if env.get("JARVIS_USER_EMAIL"):
            user += f" <{env['JARVIS_USER_EMAIL']}>"
        rows.append(
            [
                svc["id"],
                svc["deploy_status"],
                user,
                ",".join(svc["aliases"]) or "-",
                svc["created_at"],
            ]
        )
    _print_table(rows)
    return 0


def cmd_create(b: DockerBackend, args: argparse.Namespace) -> int:
    if not valid_slug(args.slug):
        print(f"Slug no válido: {args.slug!r} (a-z, 0-9 y guiones)", file=sys.stderr)
        return 2
    env = read_env_file(args.env_file)
    if not env:
        print(f"{args.env_file}: vacío o no existe", file=sys.stderr)
        return 2
    if not env.get("OPENJARVIS_API_KEY"):
        print("Falta OPENJARVIS_API_KEY en el fichero", file=sys.stderr)
        return 2
    result = b.create_container(
        args.slug, env, railway_id=args.railway_id or "", reuse_volume=True
    )
    print(f"Creado {result['name']} (volumen {result['volume']}).")
    ends = b.endpoints(args.slug, env["OPENJARVIS_API_KEY"])
    print(f"Enlace personal: {ends['link']}")
    if env.get("JARVIS_ACCESS_CODE"):
        print(f"Código de acceso: {env['JARVIS_ACCESS_CODE']}")
    return 0


def cmd_recreate(b: DockerBackend, args: argparse.Namespace) -> int:
    if args.all:
        targets = [s["id"] for s in sorted(b.services(), key=lambda s: s["id"])]
    elif args.slug:
        targets = [args.slug]
    else:
        print("Indica un slug o --all", file=sys.stderr)
        return 2
    failed = 0
    for slug in targets:
        try:
            b.recreate(slug)
            print(f"Recreado {container_name(slug)}")
        except DockerError as exc:
            failed += 1
            print(f"ERROR en {slug}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def cmd_set(b: DockerBackend, args: argparse.Namespace) -> int:
    updates = {}
    for item in args.pairs:
        key, sep, value = item.partition("=")
        if not sep or not key:
            print(f"Formato KEY=value: {item!r}", file=sys.stderr)
            return 2
        updates[key] = value
    b.recreate(args.slug, updates)
    names = ", ".join(updates)
    print(f"Actualizado {container_name(b.resolve_id(args.slug))}: {names}")
    return 0


def cmd_delete(b: DockerBackend, args: argparse.Namespace) -> int:
    svc = b.find(args.slug)
    if svc is None:
        print(f"No existe la instancia {args.slug}", file=sys.stderr)
        return 1
    if (args.confirm or "").strip().lower() != svc["id"]:
        print(
            f"Para borrar {svc['name']} y sus datos repite el slug: "
            f"--confirm {svc['id']}",
            file=sys.stderr,
        )
        return 2
    b.remove(svc["id"], keep_volume=args.keep_volume)
    print(
        f"Borrado {svc['name']}"
        + (" (volumen conservado)" if args.keep_volume else " y su volumen")
    )
    return 0


def cmd_print_labels(b: DockerBackend, args: argparse.Namespace) -> int:
    data = b.inspect(args.slug)
    if data is not None:
        labels = (data.get("Config") or {}).get("Labels") or {}
    else:
        labels = b._labels(args.slug, railway_id="", shared_refs={})
        print("# (no existe: etiquetas que tendría)")
    for key in sorted(labels):
        if key.startswith(("traefik.", "fasttrack.")) or key == LABEL_RAILWAY:
            print(f"{key}={labels[key]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m openjarvis.fasttrack.dockerctl",
        description="Instancias Jarvis en Docker (VPS)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="lista las instancias")
    c = sub.add_parser("create", help="crea/adopta una instancia")
    c.add_argument("--env-file", required=True)
    c.add_argument("--slug", required=True)
    c.add_argument("--railway-id", default="")
    r = sub.add_parser("recreate", help="recrea con la imagen actual")
    r.add_argument("slug", nargs="?")
    r.add_argument("--all", action="store_true")
    st = sub.add_parser("set", help="cambia variables (recrea)")
    st.add_argument("slug")
    st.add_argument("pairs", nargs="+", metavar="KEY=value")
    d = sub.add_parser("delete", help="borra contenedor y volumen")
    d.add_argument("slug")
    d.add_argument("--confirm", default="")
    d.add_argument("--keep-volume", action="store_true")
    pl = sub.add_parser("print-labels", help="muestra las etiquetas")
    pl.add_argument("slug")
    return p


COMMANDS = {
    "list": cmd_list,
    "create": cmd_create,
    "recreate": cmd_recreate,
    "set": cmd_set,
    "delete": cmd_delete,
    "print-labels": cmd_print_labels,
}


def main(
    argv: Optional[List[str]] = None, backend: Optional[DockerBackend] = None
) -> int:
    args = build_parser().parse_args(argv)
    b = backend or DockerBackend()
    try:
        return COMMANDS[args.cmd](b, args)
    except DockerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
