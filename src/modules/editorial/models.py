import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TenantRequiredMixin, TimestampMixin, UUIDPrimaryKeyMixin


class EstadoNoticia(str, enum.Enum):
    PENDIENTE = "pendiente"
    EN_REVISION = "en_revision"
    APROBADA = "aprobada"
    RECHAZADA = "rechazada"
    PUBLICADA = "publicada"


class Prioridad(str, enum.Enum):
    CRITICA = "critica"
    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"


class Noticia(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Registro cabeza: apunta siempre a la version vigente. El contenido real vive en NoticiaVersion."""

    __tablename__ = "noticias"

    grabacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grabaciones.id"), nullable=False, index=True
    )
    # Nullable: una Noticia podria eventualmente crearse sin pasar por el
    # pipeline automatico (ej. carga manual futura). Cuando si viene del
    # pipeline, referencia el PipelineRun que la genero.
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_runs.id"), nullable=True, index=True
    )
    estado: Mapped[EstadoNoticia] = mapped_column(
        Enum(EstadoNoticia, name="estado_noticia", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=EstadoNoticia.PENDIENTE,
    )
    # Nullable: se setea despues de insertar la primera NoticiaVersion (evita ciclo de FK al crear).
    version_actual_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("noticia_versiones.id", use_alter=True), nullable=True
    )
    clip_inicio_seg: Mapped[float] = mapped_column(Float, nullable=False)
    clip_fin_seg: Mapped[float] = mapped_column(Float, nullable=False)
    # Nullable: solo se llena si ClipStorage.upload() tuvo exito -- un fallo
    # de subida no debe tumbar el PipelineRun completo (la noticia y su
    # texto ya son validos sin el audio).
    clip_s3_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Bloqueo editorial (FR-051): mientras un periodista tiene la noticia en
    # EN_REVISION, queda asignada solo a el -- nadie mas puede editarla ni
    # aprobarla/rechazarla. Se libera (vuelve a NULL) al aprobar o rechazar.
    asignado_a: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usuarios.id"), nullable=True, index=True
    )
    asignado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    motivo_rechazo: Mapped[str | None] = mapped_column(Text, nullable=True)


class NoticiaVersion(Base, UUIDPrimaryKeyMixin):
    """Inmutable. Cada edicion -- incluso post-publicacion -- inserta una fila nueva. RN-003/FR-071."""

    __tablename__ = "noticia_versiones"

    noticia_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("noticias.id"), nullable=False, index=True
    )
    numero_version: Mapped[int] = mapped_column(Integer, nullable=False)
    titulo: Mapped[str] = mapped_column(String(500), nullable=False)
    resumen: Mapped[str] = mapped_column(Text, nullable=False)
    transcripcion_texto: Mapped[str] = mapped_column(Text, nullable=False)
    tema_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("temas.id"), nullable=True)
    subtema_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("subtemas.id"), nullable=True)
    ai_score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-100, solo referencia (RN-008)
    prioridad: Mapped[Prioridad | None] = mapped_column(
        Enum(Prioridad, name="prioridad", values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    confianza: Mapped[dict] = mapped_column(JSONB, default=dict)  # confianza por campo, solo referencia
    # keywords/news_type/people/organizations/locations crudos del LLM -- sin
    # resolver contra el catalogo de Entidad todavia (ver NoticiaVersionEntidad,
    # esa deduplicacion/matching es un paso posterior, fuera de alcance aqui).
    metadatos_ia: Mapped[dict] = mapped_column(JSONB, default=dict)
    es_generada_por_ia: Mapped[bool] = mapped_column(Boolean, default=True)
    # NULL = version generada por IA sin intervencion humana todavia.
    editado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("noticia_id", "numero_version"),)


class NoticiaVersionEntidad(Base):
    """Junction: entidades mencionadas en una version especifica de una noticia."""

    __tablename__ = "noticia_version_entidades"

    noticia_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("noticia_versiones.id"), primary_key=True
    )
    entidad_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entidades.id"), primary_key=True)


class Sentimiento(str, enum.Enum):
    POSITIVO = "positivo"
    NEGATIVO = "negativo"
    NEUTRO = "neutro"


class EstadoClienteNoticia(str, enum.Enum):
    SUGERIDA = "sugerida"      # propuesta por el matching automatico de IA
    DESCARTADA = "descartada"  # el periodista la quito de la lista antes de aprobar (se conserva para KPI de precision)
    CONFIRMADA = "confirmada"  # quedo en la lista curada, pendiente del Aprobar general de la noticia
    PUBLICADA = "publicada"    # la noticia fue aprobada y esta visible en el portal de este cliente


class ClienteNoticia(Base, UUIDPrimaryKeyMixin, TenantRequiredMixin, TimestampMixin):
    """Relacion Cliente-Noticia (FR-012). Una fila por (tenant, noticia) sugerido o confirmado."""

    __tablename__ = "cliente_noticias"

    noticia_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("noticias.id"), nullable=False, index=True
    )
    sentimiento: Mapped[Sentimiento | None] = mapped_column(
        Enum(Sentimiento, name="sentimiento", values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    estado: Mapped[EstadoClienteNoticia] = mapped_column(
        Enum(
            EstadoClienteNoticia,
            name="estado_cliente_noticia",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=EstadoClienteNoticia.SUGERIDA,
    )
    fecha_publicacion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("tenant_id", "noticia_id"),)


class MonitoringProfile(Base, UUIDPrimaryKeyMixin, TenantRequiredMixin, TimestampMixin):
    """Configuracion por tenant (FR-011) que alimenta el matching automatico cliente-noticia."""

    __tablename__ = "monitoring_profiles"

    personas_interes: Mapped[list] = mapped_column(JSONB, default=list)
    instituciones: Mapped[list] = mapped_column(JSONB, default=list)
    temas: Mapped[list] = mapped_column(JSONB, default=list)
    medios: Mapped[list] = mapped_column(JSONB, default=list)  # lista de medio_id
    destinatarios_informe: Mapped[list] = mapped_column(JSONB, default=list)  # emails

    __table_args__ = (UniqueConstraint("tenant_id"),)


class EtiquetadoPrivado(Base, UUIDPrimaryKeyMixin, TenantRequiredMixin):
    """FR-085. Nunca visible fuera del tenant; no genera aviso a AdSignal (RN-009)."""

    __tablename__ = "etiquetados_privados"

    cliente_noticia_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cliente_noticias.id"), nullable=False, index=True
    )
    subcategoria: Mapped[str | None] = mapped_column(String(255), nullable=True)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
