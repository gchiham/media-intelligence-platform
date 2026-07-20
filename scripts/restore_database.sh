#!/bin/bash
# Restaura un backup generado por backup_database.sh. DESTRUCTIVO: reemplaza
# los datos actuales de la base -- pide confirmacion explicita.
#
# Uso: ./scripts/restore_database.sh postgres_backups/media_intelligence_20260101_120000.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -ne 1 ]; then
  echo "Uso: $0 <archivo_backup.sql.gz>" >&2
  exit 1
fi
FILE="$1"

if [ ! -f "$FILE" ]; then
  echo "Error: no existe $FILE" >&2
  exit 1
fi
if [ ! -f .env ]; then
  echo "Error: falta .env" >&2
  exit 1
fi
set -a
source .env
set +a

echo "ADVERTENCIA: esto reemplaza TODOS los datos actuales de la base '${POSTGRES_DB:-media_intelligence}' con el contenido de $FILE."
read -r -p "Escribe 'si' para continuar: " CONFIRM
if [ "$CONFIRM" != "si" ]; then
  echo "Cancelado."
  exit 0
fi

echo "==> Restaurando $FILE ..."
gunzip -c "$FILE" | docker compose exec -T postgres psql -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-media_intelligence}"

echo "==> Restauracion completa."
