#!/bin/bash
# Refresca las menciones del frontend Kronos: consulta la DB desde el
# contenedor del backend y empalma el resultado en el bundle estatico.
# Idempotente; corre por cron cada 30 min. Si la consulta falla o devuelve
# pocas menciones, el bundle NO se toca (mejor datos de hace 30 min que nada).
set -euo pipefail
cd /home/ubuntu/kronos_refresh

docker exec -i media-intelligence-platform-backend-1 python3 - \
  < generar_menciones.py > /tmp/menciones_kronos.tmp
mv /tmp/menciones_kronos.tmp /tmp/menciones_kronos.json
python3 parchar_bundle.py /tmp/menciones_kronos.json
