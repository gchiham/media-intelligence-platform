import uuid
from datetime import datetime

from sqlalchemy import select

from src.infrastructure.db.repository import Repository
from src.modules.recordings.models import EstadoGrabacion, Grabacion, Transcripcion


class GrabacionRepository(Repository[Grabacion]):
    model = Grabacion

    def get_by_s3_key(self, s3_key: str) -> Grabacion | None:
        stmt = select(Grabacion).where(Grabacion.s3_key == s3_key)
        return self._session.scalars(stmt).first()

    def list_pendientes(
        self,
        limit: int = 100,
        programa_id: uuid.UUID | None = None,
        fecha_desde: datetime | None = None,
        fecha_hasta: datetime | None = None,
    ) -> list[Grabacion]:
        stmt = select(Grabacion).where(Grabacion.estado == EstadoGrabacion.PENDIENTE)
        if programa_id is not None:
            stmt = stmt.where(Grabacion.programa_id == programa_id)
        if fecha_desde is not None:
            stmt = stmt.where(Grabacion.fecha_inicio >= fecha_desde)
        if fecha_hasta is not None:
            stmt = stmt.where(Grabacion.fecha_inicio < fecha_hasta)
        stmt = stmt.order_by(Grabacion.fecha_inicio.asc()).limit(limit)
        return list(self._session.scalars(stmt))


class TranscripcionRepository(Repository[Transcripcion]):
    model = Transcripcion

    def get_by_grabacion_id(self, grabacion_id: uuid.UUID) -> Transcripcion | None:
        stmt = select(Transcripcion).where(Transcripcion.grabacion_id == grabacion_id)
        return self._session.scalars(stmt).first()
