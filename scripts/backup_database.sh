#!/bin/bash
# Backup de PostgreSQL via pg_dump, corrido dentro del contenedor `postgres`
# ya en marcha. Escribe local en postgres_backups/ -- no sube a S3 todavia
# (fuera de alcance de esta fase, ver docs/DEPLOYMENT.md).
#
# Uso: ./scripts/backup_database.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Error: falta .env" >&2
  exit 1
fi
set -a
source .env
set +a

BACKUP_DIR="postgres_backups"
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILE="$BACKUP_DIR/${POSTGRES_DB:-media_intelligence}_${TIMESTAMP}.sql.gz"

echo "==> Generando backup en $FILE ..."
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-media_intelligence}" | gzip > "$FILE"

echo "==> Backup completo: $FILE ($(du -h "$FILE" | cut -f1))"
