import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TenantRequiredMixin, TimestampMixin, UUIDPrimaryKeyMixin


class EstadoInforme(str, enum.Enum):
    BORRADOR = "borrador"
    ENVIADO = "enviado"


class InformeSemanal(Base, UUIDPrimaryKeyMixin, TenantRequiredMixin, TimestampMixin):
    __tablename__ = "informes_semanales"

    semana_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    semana_fin: Mapped[date] = mapped_column(Date, nullable=False)
    estado: Mapped[EstadoInforme] = mapped_column(
        Enum(EstadoInforme, name="estado_informe", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=EstadoInforme.BORRADOR,
    )
    resumen_texto: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enviado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    enviado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InformeNoticia(Base):
    """Junction: que noticias entran en el consolidado semanal, y si el periodista las quito del borrador."""

    __tablename__ = "informe_noticias"

    informe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("informes_semanales.id"), primary_key=True
    )
    noticia_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("noticias.id"), primary_key=True)
    incluida: Mapped[bool] = mapped_column(Boolean, default=True)
