"""``python -m openjarvis.fasttrack [--host H] [--port P]``: run the console."""

from __future__ import annotations

import argparse
import os

import uvicorn

from openjarvis.fasttrack.admin import create_admin_app


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Fast Track admin console")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()
    uvicorn.run(
        create_admin_app(),
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
