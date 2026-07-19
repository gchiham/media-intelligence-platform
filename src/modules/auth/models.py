import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RolUsuario(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    SUPERVISOR_EDITORIAL = "supervisor_editorial"
    PERIODISTA = "periodista"
    ADMIN_CLIENTE = "admin_cliente"
    USUARIO_CLIENTE = "usuario_cliente"


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenants"

    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    rtn: Mapped[str | None] = mapped_column(String(50))
    contactos: Mapped[dict] = mapped_column(JSONB, default=dict)


class Usuario(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "usuarios"

    # NULL = staff interno de AdSignal (super_admin, supervisor_editorial, periodista) -> cross-tenant.
    # No-NULL = admin_cliente / usuario_cliente, acotado a un tenant.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[RolUsuario] = mapped_column(
        Enum(RolUsuario, name="rol_usuario", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    activo: Mapped[bool] = mapped_column(default=True)


class LoginEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "login_events"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=False, index=True
    )
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
