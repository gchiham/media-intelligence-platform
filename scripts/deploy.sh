#!/bin/bash
# Despliegue inicial o actualizacion de version: reconstruye la imagen del
# backend (recoge el codigo mas reciente) y levanta/actualiza todo el stack.
# Las migraciones corren solas al arrancar el backend (ver docker/entrypoint.sh).
#
# Uso: ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Error: falta .env -- copia .env.example a .env y completa los valores." >&2
  exit 1
fi

echo "==> Construyendo imagen del backend..."
docker compose build backend

echo "==> Levantando el stack (postgres, backend, nginx)..."
docker compose up -d

echo "==> Esperando a que el backend quede saludable (hasta 60s)..."
healthy=""
for _ in $(seq 1 30); do
  status=$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose ps -q backend)" 2>/dev/null || echo "")
  if [ "$status" = "healthy" ]; then
    healthy="1"
    break
  fi
  sleep 2
done

if [ -z "$healthy" ]; then
  echo "Advertencia: el backend no reporto 'healthy' a tiempo -- revisa los logs:" >&2
  echo "  docker compose logs backend" >&2
else
  echo "Backend saludable."
fi

echo "==> Estado del stack:"
docker compose ps
