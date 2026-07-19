import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TipoMedio(str, enum.Enum):
    RADIO = "radio"
    TV = "tv"


class Medio(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Medios monitoreados. Globales (no por tenant): la captura es infraestructura compartida."""

    __tablename__ = "medios"

    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo: Mapped[TipoMedio] = mapped_column(
        Enum(TipoMedio, name="tipo_medio", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    # Identificador del sistema de captura externo (stream_id). Es el prefijo de carpeta
    # en el bucket S3, ej. "hch_radio", "canal_11". Fuente de verdad: config/stations.json
    # del repo mediaCAP.
    codigo: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)


class Programa(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "programas"

    medio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medios.id"), nullable=False, index=True)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    horario: Mapped[str | None] = mapped_column(String(100))
