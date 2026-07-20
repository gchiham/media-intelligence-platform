# Imagen del backend (FastAPI). No cambia nada del dominio ni de la
# arquitectura -- solo empaqueta lo que ya existe para correr en produccion.
FROM python:3.12-slim

# ffmpeg: MediaProcessingOrchestrator invoca `clip_audio` como subprocess al
# ejecutar un pipeline run (POST /api/v1/pipeline/process corre en el mismo
# proceso del backend, ver docs/BACKEND_ARCHITECTURE.md) -- sin esto, ese
# endpoint fallaria en produccion. curl: lo usa el HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
