#!/bin/sh
# Entrypoint del backend en produccion: corre las migraciones existentes
# (nunca cambia el esquema por su cuenta -- solo aplica lo que ya esta en
# alembic/versions/) y despues levanta Uvicorn. Si `alembic upgrade head`
# falla, el contenedor no arranca -- mejor eso que servir con un esquema
# desactualizado.
set -e

echo "[entrypoint] esperando a que Postgres acepte conexiones..."
python -c "
import time
from sqlalchemy import create_engine
from src.infrastructure.config import settings

for intento in range(30):
    try:
        create_engine(settings.database_url).connect().close()
        print('[entrypoint] Postgres listo')
        break
    except Exception as exc:
        print(f'[entrypoint] Postgres no disponible todavia ({exc}); reintentando...')
        time.sleep(2)
else:
    raise SystemExit('[entrypoint] Postgres no respondio a tiempo')
"

echo "[entrypoint] corriendo migraciones (alembic upgrade head)..."
alembic upgrade head

echo "[entrypoint] iniciando Uvicorn..."
exec uvicorn src.api.main:app --host 0.0.0.0 --port 8000
