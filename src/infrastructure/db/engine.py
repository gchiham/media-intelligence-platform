from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.infrastructure.config import settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(settings.database_url)


def get_session() -> Session:
    SessionLocal = sessionmaker(bind=get_engine())
    return SessionLocal()


@lru_cache
def get_coverage_engine() -> Engine:
    """DB externa del sistema capturador (Destroyer), solo lectura -- rol
    coverage_reader_discoverysvc, acotado a recording_coverage. Ver
    CoverageDiscoveryService."""
    if not settings.database_url_coverage:
        raise RuntimeError("falta DATABASE_URL_COVERAGE en .env")
    return create_engine(settings.database_url_coverage)


def get_coverage_session() -> Session:
    SessionLocal = sessionmaker(bind=get_coverage_engine())
    return SessionLocal()
