#!/bin/bash
# Arranca el stack sin reconstruir imagenes (para eso es deploy.sh). Uso
# tipico: despues de un reboot del servidor, o tras un `stop.sh`.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d
docker compose ps
