"""Run an AI Fast Track service.

``python -m openjarvis.fasttrack [admin|landing] [--host H] [--port P]``

* ``admin`` (default): the console that provisions user instances.
* ``landing``: the public product site with "access my Jarvis".
"""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Fast Track services")
    parser.add_argument(
        "role", nargs="?", default="admin", choices=["admin", "landing"]
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()
    if args.role == "landing":
        from openjarvis.fasttrack.landing import create_landing_app

        app = create_landing_app()
    else:
        from openjarvis.fasttrack.admin import create_admin_app

        app = create_admin_app()
    # Client IPs come from X-Real-IP (web_session.client_ip); the scheme
    # from X-Forwarded-Proto, so cookies are marked Secure behind Railway.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
