import uuid

from sqlalchemy import select

from src.infrastructure.db.repository import Repository
from src.modules.media.models import Medio, Programa


class MedioRepository(Repository[Medio]):
    """MediaSource."""

    model = Medio

    def get_by_codigo(self, codigo: str) -> Medio | None:
        stmt = select(Medio).where(Medio.codigo == codigo)
        return self._session.scalars(stmt).first()


class ProgramaRepository(Repository[Programa]):
    """Program."""

    model = Programa

    def get_first_by_medio_id(self, medio_id: uuid.UUID) -> Programa | None:
        """Discovery necesita *un* programa_id por Grabacion, pero todavia no
        hay horarios reales por programa (docs/INGESTION_DESIGN.md) -- cada
        Medio tiene un unico Programa "catch-all" sembrado por
        scripts/seed_medios.py, y esto devuelve ese."""
        stmt = select(Programa).where(Programa.medio_id == medio_id).limit(1)
        return self._session.scalars(stmt).first()
