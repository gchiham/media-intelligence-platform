import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, UUIDPrimaryKeyMixin


class Auditoria(Base, UUIDPrimaryKeyMixin):
    """Bitacora generica cross-modulo. FR-072 / NFR de auditoria.

    NoticiaVersion ya es su propia bitacora inmutable para noticias (mas rica: guarda
    el contenido completo por version). Esta tabla es para todo lo demas: logins, envios
    de informes, cambios de configuracion, altas/bajas de usuarios y clientes, etc.
    """

    __tablename__ = "auditoria"

    tabla: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    registro_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    accion: Mapped[str] = mapped_column(String(100), nullable=False)
    valor_anterior: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    valor_nuevo: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
