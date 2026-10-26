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

# Same image, second role: the AI Fast Track admin console (no volume,
# no WhatsApp). It provisions the per-user instances through Railway's API.
if [ "${JARVIS_ROLE:-}" = "admin" ]; then
  if [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid=openjarvis --regid=openjarvis --init-groups       python3 -m openjarvis.fasttrack --port "$PORT"
  fi
  exec python3 -m openjarvis.fasttrack --port "$PORT"
fi

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

$RUN_AS python3 /usr/local/lib/openjarvis/bootstrap_config.py

exec $RUN_AS jarvis serve --host 0.0.0.0 --port "$PORT" \
  --engine "$JARVIS_ENGINE" --model "$JARVIS_MODEL"
