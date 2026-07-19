from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/media_intelligence"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"


settings = Settings()
