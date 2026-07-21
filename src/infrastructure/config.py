from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/media_intelligence"
    # Claude es el LLM primario para segmentacion (FR-041); OpenAI queda como
    # respaldo si Claude agota sus reintentos -- ver AIProviderWithFallback.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5"
    # Fallback de segmentacion (AIProviderWithFallback): mismo proveedor
    # (Anthropic), modelo mas chico -- ver deps.py. OpenAI dejo de ser el
    # secundario porque esa cuenta se quedo sin fondos (insufficient_quota),
    # asi que dependia de un proveedor que en la practica nunca respondia.
    anthropic_fallback_model: str = "claude-haiku-4-5-20251001"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    # Directorio local donde el pipeline busca los archivos de una Grabacion
    # (words.json + audio) y donde escribe los clips generados. Ver
    # RecordingResolver en src/modules/pipeline/resolvers.py -- "local" en
    # dev, "s3" en produccion (S3RecordingResolver, docs/INGESTION_DESIGN.md).
    local_media_dir: Path = Path("./data/recordings")
    recording_resolver: str = "local"

    # Ingesta S3 -> Postgres (DiscoveryService/QueueService/consumers). Ver
    # docs/INGESTION_DESIGN.md. Buckets y colas ya existen en AWS (infra
    # provisionada fuera de este repo, ver docs/INFRASTRUCTURE.md).
    capture_bucket: str = "mediadev-recordings"
    transcribe_output_bucket: str = "media-intel-transcribe-050871635829"
    transcription_jobs_queue_url: str = ""
    transcription_done_queue_url: str = ""
    transcription_dlq_url: str = ""
    aws_region: str = "us-east-1"

    # Solo lectura contra la DB del sistema capturador (Destroyer), tabla
    # recording_coverage -- ver CoverageDiscoveryService. None hasta que se
    # configure explicitamente (no todos los entornos la necesitan).
    database_url_coverage: str | None = None


settings = Settings()
