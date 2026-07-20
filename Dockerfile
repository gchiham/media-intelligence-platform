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
# scripts/ (seed_medios.py, discover_grabaciones.py, enqueue_transcriptions.py,
# consume_transcription_results.py -- docs/INGESTION_DESIGN.md) se corren via
# `docker compose exec backend python scripts/...`, no se instalan como
# paquete. worker_prefetch.py tambien vive aca pero es para chepita, no se
# ejecuta desde este contenedor -- se queda por simplicidad de un solo
# directorio scripts/ en el repo.
COPY scripts ./scripts

RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
