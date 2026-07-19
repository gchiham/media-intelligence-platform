from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/media_intelligence"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    # Directorio local donde el pipeline busca los archivos de una Grabacion
    # (words.json + audio) y donde escribe los clips generados. Ver
    # RecordingResolver en src/modules/pipeline/resolvers.py -- cuando exista
    # integracion con S3, esto deja de usarse sin que el dominio cambie.
    local_media_dir: Path = Path("./data/recordings")


settings = Settings()
