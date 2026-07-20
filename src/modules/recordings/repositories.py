import uuid

from sqlalchemy import select

from src.infrastructure.db.repository import Repository
from src.modules.recordings.models import EstadoGrabacion, Grabacion, Transcripcion


class GrabacionRepository(Repository[Grabacion]):
    model = Grabacion

    def get_by_s3_key(self, s3_key: str) -> Grabacion | None:
        stmt = select(Grabacion).where(Grabacion.s3_key == s3_key)
        return self._session.scalars(stmt).first()

    def list_pendientes(self, limit: int = 100) -> list[Grabacion]:
        stmt = (
            select(Grabacion)
            .where(Grabacion.estado == EstadoGrabacion.PENDIENTE)
            .order_by(Grabacion.fecha_inicio.asc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class TranscripcionRepository(Repository[Transcripcion]):
    model = Transcripcion

    def get_by_grabacion_id(self, grabacion_id: uuid.UUID) -> Transcripcion | None:
        stmt = select(Transcripcion).where(Transcripcion.grabacion_id == grabacion_id)
        return self._session.scalars(stmt).first()
