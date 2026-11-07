#!/bin/sh
# Actualiza Jarvis en el VPS: trae la rama, reconstruye la imagen y recrea
# la web, la consola y todas las instancias (mismas variables y volúmenes).
#
#   sh deploy/vps/update.sh            # rama vps-docker
#   JARVIS_BRANCH=main sh deploy/vps/update.sh
#
# Cada instancia se para antes de arrancar la nueva (nunca dos WhatsApp a
# la vez); tienen unos segundos de corte.
set -eu

BRANCH="${JARVIS_BRANCH:-vps-docker}"
IMAGE="${JARVIS_IMAGE:-openjarvis:latest}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE="docker compose -f $ROOT/deploy/vps/docker-compose.yml"
cd "$ROOT"

echo "==> git: $BRANCH"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "==> imagen $IMAGE"
# La anterior queda como :previous para volver atrás si hiciera falta:
#   docker tag openjarvis:previous openjarvis:latest && sh deploy/vps/update.sh
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker tag "$IMAGE" "${IMAGE%%:*}:previous"
fi
docker build -t "$IMAGE" -f deploy/railway/Dockerfile .

echo "==> web y consola"
$COMPOSE up -d --force-recreate landing console
# Espera a que la consola (nueva) responda antes de usar dockerctl.
i=0
until $COMPOSE exec -T console python3 -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" \
  >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -ge 30 ] && { echo "La consola no arranca"; exit 1; }
  sleep 2
done

echo "==> instancias"
$COMPOSE exec -T console python3 -m openjarvis.fasttrack.dockerctl recreate --all
$COMPOSE exec -T console python3 -m openjarvis.fasttrack.dockerctl list

echo "Hecho."
