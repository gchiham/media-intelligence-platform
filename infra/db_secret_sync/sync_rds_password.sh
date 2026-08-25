#!/bin/bash
# Sincroniza la password de RDS en el .env del servidor.
#
# La instancia media-intel-postgres tiene ManageMasterUserPassword: AWS le rota
# la password sola cada 7 dias (la ultima rotacion, 25-ago 06:09 GMT-6, tumbo
# el pipeline entero: enqueue, discover, rss_ingest y el refresco de Kronos
# quedaron con "password authentication failed"). Este script cierra ese hueco:
# lee el secreto gestionado, y si la URL cambio la escribe en .env y recrea el
# backend. Si no cambio no toca nada, asi que es seguro correrlo por cron.
#
# Cron de `ubuntu`, cada 10 min:
#   */10 * * * * /home/ubuntu/db_secret_sync/sync_rds_password.sh >> /home/ubuntu/db_secret_sync/sync.log 2>&1
set -euo pipefail
PROYECTO=/home/ubuntu/media-intelligence-platform
DIR=/home/ubuntu/db_secret_sync
cd "$PROYECTO"

# El fetch va por un contenedor efimero de la imagen del backend (el host no
# tiene AWS CLI; la imagen si trae boto3 y toma las credenciales del instance
# profile). Efimero y no `compose exec` a proposito: si la password quedo mal,
# el backend esta en crash-loop y `exec` no engancha -- justo cuando mas se
# necesita este script.
URL=$(docker run --rm -i --entrypoint python3 media-intelligence-platform-backend -   < "$DIR/sync_rds_password.py")
if [[ "$URL" != postgresql+psycopg://* ]]; then
  echo "$(date -Is) secreto ilegible, no toco .env" >&2
  exit 1
fi

ACTUAL=$(grep -m1 '^RDS_DATABASE_URL=' .env | cut -d= -f2-)
if [[ "$ACTUAL" == "$URL" ]]; then
  exit 0
fi

cp .env "$DIR/env.bak.$(date +%Y%m%d%H%M%S)"
# python y no sed: la password trae `#`, `?`, `&` y demas que sed interpreta.
URL="$URL" python3 - <<'PY'
import os
from pathlib import Path

url = os.environ["URL"]
ruta = Path("/home/ubuntu/media-intelligence-platform/.env")
lineas = []
for linea in ruta.read_text(encoding="utf-8").splitlines(keepends=True):
    fin = "\n" if linea.endswith("\n") else ""
    if linea.startswith("RDS_DATABASE_URL=") or linea.startswith("DATABASE_URL="):
        clave = linea.split("=", 1)[0]
        lineas.append(f"{clave}={url}{fin}")
    else:
        lineas.append(linea)
ruta.write_text("".join(lineas), encoding="utf-8")
PY

docker compose up -d backend
echo "$(date -Is) password de RDS rotada: .env actualizado y backend recreado"
