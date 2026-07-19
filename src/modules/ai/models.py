import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Tema(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "temas"

    nombre: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


class Subtema(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "subtemas"

    tema_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("temas.id"), nullable=False, index=True)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)


class TipoEntidad(str, enum.Enum):
    PERSONA = "persona"
    INSTITUCION = "institucion"
    EMPRESA = "empresa"
    LUGAR = "lugar"


class Entidad(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Catalogo global reutilizable de entidades detectadas por la IA (editable por periodistas)."""

    __tablename__ = "entidades"

    tipo: Mapped[TipoEntidad] = mapped_column(
        Enum(TipoEntidad, name="tipo_entidad", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
