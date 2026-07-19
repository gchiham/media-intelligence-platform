"""PipelineRun: registro de una ejecucion del pipeline de IA (transcripcion ->
segmentacion -> clipping) sobre una Grabacion. El Backend NUNCA ejecuta ese
pipeline el mismo -- solo lo administra: sabe que corrio, cuando, con que
resultado. La ejecucion real ocurre en chepita (transcripcion) y en el
orquestador (MediaProcessingOrchestrator, segmentacion + clipping), fuera de
este proceso. Ver docs/BACKEND_ARCHITECTURE.md.

Nombre del modelo en ingles (`PipelineRun`) a diferencia del resto del dominio
(que sigue el espanol de docs/PRD.md, ej. Noticia/Medio/Programa) -- es un
registro tecnico-operativo, no un termino del lenguaje ubicuo editorial. Ver
la nota de naming en BACKEND_ARCHITECTURE.md.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EstadoPipelineRun(str, enum.Enum):
    PENDIENTE = "pendiente"
    EN_PROGRESO = "en_progreso"
    COMPLETADO = "completado"
    ERROR = "error"


class PipelineRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "pipeline_runs"

    grabacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grabaciones.id"), nullable=False, index=True
    )
    estado: Mapped[EstadoPipelineRun] = mapped_column(
        Enum(EstadoPipelineRun, name="estado_pipeline_run", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=EstadoPipelineRun.PENDIENTE,
    )
    iniciado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalizado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    noticias_generadas: Mapped[int] = mapped_column(Integer, default=0)
    error_mensaje: Mapped[str | None] = mapped_column(Text, nullable=True)
    # No se llama "metadata" -- ese nombre lo reserva SQLAlchemy en Base.metadata.
    # Datos libres del procesamiento: proveedor de IA/modelo usado, batch_size,
    # padding_seconds, duracion de cada paso, etc. Ver docs/BACKEND_ARCHITECTURE.md.
    metadatos: Mapped[dict] = mapped_column(JSONB, default=dict)
