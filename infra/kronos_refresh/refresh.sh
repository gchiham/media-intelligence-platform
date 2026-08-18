#!/bin/bash
# Refresca las menciones del frontend Kronos: consulta la DB desde el
# contenedor del backend y empalma el resultado en el bundle estatico.
# Idempotente; corre por cron cada 30 min como el usuario `ubuntu`.
# Los temporales van al directorio propio y NO a /tmp: una corrida manual
# como root dejaba el .json de root y el cron moria con "Operation not
# permitted" en cada tick. Si la consulta falla o devuelve
# pocas menciones, el bundle NO se toca (mejor datos de hace 30 min que nada).
set -euo pipefail
cd /home/ubuntu/kronos_refresh

docker exec -i media-intelligence-platform-backend-1 python3 - \
  < generar_menciones.py > /home/ubuntu/kronos_refresh/menciones.tmp
mv /home/ubuntu/kronos_refresh/menciones.tmp /home/ubuntu/kronos_refresh/menciones.json
python3 parchar_bundle.py /home/ubuntu/kronos_refresh/menciones.json
