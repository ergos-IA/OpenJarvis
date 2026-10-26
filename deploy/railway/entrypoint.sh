#!/bin/sh
# Railway entrypoint for one Jarvis instance (or, with JARVIS_ROLE=admin,
# the AI Fast Track admin console).
#
#  1. Ensures $OPENJARVIS_HOME (the Railway volume) is writable by the
#     unprivileged openjarvis user -- Railway mounts volumes owned by root.
#  2. Renders config.toml from environment variables (bootstrap_config.py).
#  3. Starts `jarvis serve` on Railway's $PORT as the openjarvis user.
set -eu

: "${OPENJARVIS_HOME:=/data}"
: "${PORT:=8000}"
: "${JARVIS_ENGINE:=cloud}"
: "${JARVIS_MODEL:=gpt-4o-mini}"
export OPENJARVIS_HOME

# Same image, other roles (no volume, no WhatsApp): JARVIS_ROLE=admin is the
# AI Fast Track console that provisions user instances through Railway's API;
# JARVIS_ROLE=landing is the public product site that routes users by code.
case "${JARVIS_ROLE:-}" in
  admin|landing)
    if [ "$(id -u)" = "0" ]; then
      exec setpriv --reuid=openjarvis --regid=openjarvis --init-groups         python3 -m openjarvis.fasttrack "$JARVIS_ROLE" --port "$PORT"
    fi
    exec python3 -m openjarvis.fasttrack "$JARVIS_ROLE" --port "$PORT"
    ;;
esac

if [ -z "${OPENJARVIS_API_KEY:-}" ]; then
  echo "ERROR: OPENJARVIS_API_KEY no está definida. Genera una clave y añádela" >&2
  echo "       como variable del servicio (ver deploy/railway/README.md)." >&2
  exit 1
fi

mkdir -p "$OPENJARVIS_HOME"
if [ "$(id -u)" = "0" ]; then
  chown -R openjarvis:openjarvis "$OPENJARVIS_HOME"
  RUN_AS="setpriv --reuid=openjarvis --regid=openjarvis --init-groups"
else
  RUN_AS=""
fi

# bootstrap_config.py --serve renders config.toml (applying the user's own AI
# settings from the volume, if any) and execs `jarvis serve`.  The server
# re-execs the same launcher to apply new AI settings.
export JARVIS_LAUNCHER=/usr/local/lib/openjarvis/bootstrap_config.py
exec $RUN_AS python3 "$JARVIS_LAUNCHER" --serve
