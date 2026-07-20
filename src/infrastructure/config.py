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
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    # Directorio local donde el pipeline busca los archivos de una Grabacion
    # (words.json + audio) y donde escribe los clips generados. Ver
    # RecordingResolver en src/modules/pipeline/resolvers.py -- cuando exista
    # integracion con S3, esto deja de usarse sin que el dominio cambie.
    local_media_dir: Path = Path("./data/recordings")


settings = Settings()
