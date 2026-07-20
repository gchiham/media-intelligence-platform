import uuid

from sqlalchemy import select

from src.infrastructure.db.repository import Repository
from src.modules.pipeline.models import EstadoPipelineRun, PipelineRun


class PipelineRunRepository(Repository[PipelineRun]):
    model = PipelineRun

    def get_completado_by_grabacion_id(self, grabacion_id: uuid.UUID) -> PipelineRun | None:
        stmt = (
            select(PipelineRun)
            .where(
                PipelineRun.grabacion_id == grabacion_id,
                PipelineRun.estado == EstadoPipelineRun.COMPLETADO,
            )
            .order_by(PipelineRun.finalizado_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()
