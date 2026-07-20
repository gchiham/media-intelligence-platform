#!/bin/bash
# Detiene el stack sin borrar volumenes -- postgres_data y ./data/./logs
# quedan intactos. Para levantar de nuevo: ./scripts/start.sh
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose down
