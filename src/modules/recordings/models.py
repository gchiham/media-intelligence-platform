import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EstadoGrabacion(str, enum.Enum):
    PENDIENTE = "pendiente"
    PROCESANDO = "procesando"
    PROCESADA = "procesada"
    ERROR = "error"


class Grabacion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Una fila por archivo de audio horario detectado en el S3 del sistema de captura externo."""

    __tablename__ = "grabaciones"

    programa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("programas.id"), nullable=False, index=True
    )
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    fecha_inicio: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fecha_fin: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    estado: Mapped[EstadoGrabacion] = mapped_column(
        Enum(EstadoGrabacion, name="estado_grabacion", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=EstadoGrabacion.PENDIENTE,
    )


class Transcripcion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Transcripcion completa de una grabacion, generada por el TranscriptionProvider."""

    __tablename__ = "transcripciones"

    grabacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grabaciones.id"), nullable=False, unique=True, index=True
    )
    texto_completo: Mapped[str] = mapped_column(nullable=False)
    segmentos: Mapped[dict] = mapped_column(JSONB, default=dict)  # timestamps por palabra/frase
    proveedor: Mapped[str] = mapped_column(String(100), nullable=False)
