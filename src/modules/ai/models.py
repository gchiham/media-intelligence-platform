import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
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

    # Forma canonica para buscar: sin acentos, minusculas, sin puntuacion ni
    # titulos ("Lic.", "Presidente"). Es lo que hace que "JOH",
    # "Juan Orlando Hernandez" y "Juan Orlando Hernández" colapsen a una sola
    # fila en vez de tres. Ver src/modules/ai/entity_resolution.py.
    nombre_normalizado: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Variantes normalizadas que apuntan a esta misma entidad (siglas,
    # apodos, orden invertido de apellidos). Lista de strings ya normalizados.
    # Se guarda aca en vez de en una tabla aparte porque el volumen por
    # entidad es chico (unidades, no miles) y siempre se lee completo junto
    # con la entidad -- una tabla hija solo agregaria un join sin beneficio.
    alias: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Cuantas veces se la menciono. Sirve para dos cosas: priorizar que
    # entidades curar a mano primero, y alimentar el vocabulario de
    # transcripcion (src/modules/transcription/vocabulary.py) con los nombres
    # que mas aparecen al aire, que son los que mas duele que Whisper falle.
    menciones: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("tipo", "nombre_normalizado"),)


class EstadoSegmentationBatch(str, enum.Enum):
    ENVIADO = "enviado"
    COMPLETADO = "completado"
    ERROR = "error"


class SegmentationBatch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Un envio a la Batch API de Anthropic (ver src/modules/ai/batch.py).

    Nombre en ingles a proposito, igual que `PipelineRun`: es un registro
    tecnico-operativo del proveedor de IA, no un termino del lenguaje ubicuo
    editorial. Ver la nota de naming en docs/BACKEND_ARCHITECTURE.md.
    """

    __tablename__ = "segmentation_batches"

    anthropic_batch_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    estado: Mapped[EstadoSegmentationBatch] = mapped_column(
        Enum(
            EstadoSegmentationBatch,
            name="estado_segmentation_batch",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=EstadoSegmentationBatch.ENVIADO,
        index=True,
    )
    modelo: Mapped[str] = mapped_column(String(100), nullable=False)
    total_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    # custom_id -> [lo, hi]. Hace falta al recolectar para validar que el
    # modelo no haya inventado indices fuera del chunk que realmente vio; para
    # entonces los chunks originales ya no estan en memoria.
    rangos: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error_mensaje: Mapped[str | None] = mapped_column(Text, nullable=True)


class SegmentationCache(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Segmentos ya calculados para una Grabacion, pendientes de que el
    pipeline los use para clipping/persistencia.

    Separar "el LLM ya respondio" de "ya se generaron las Noticias" permite
    que el batch (barato, hasta 24 h) corra desacoplado del clipping (que
    necesita bajar el audio y correr ffmpeg, y puede fallar o reanudarse sin
    volver a pagar la inferencia).
    """

    __tablename__ = "segmentation_cache"

    grabacion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("grabaciones.id"), nullable=False, unique=True, index=True
    )
    segmentos: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    modelo: Mapped[str] = mapped_column(String(100), nullable=False)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("segmentation_batches.id"), nullable=True, index=True
    )
    consumido: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class ContenidoRepetido(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Bloques de texto que se repiten identicos al aire -- publicidad, cortinas,
    promos de programacion.

    Motivacion (docs/EFFICIENCY_REVIEW.md §3): solo ~50% del aire es noticia,
    pero hoy el 100% se manda al LLM y se paga para descubrir que no habia
    nada. Un anuncio se emite decenas de veces al dia con el mismo guion, asi
    que su transcripcion es casi identica cada vez: se detecta por huella y se
    salta a partir de la N-esima aparicion.

    Es deteccion por TEXTO, no huella acustica. Ahorra la llamada al LLM (lo
    caro) pero no el GPU, porque para tener el texto ya hubo que transcribir.
    La huella acustica pre-transcripcion queda como paso siguiente.
    """

    __tablename__ = "contenido_repetido"

    # sha256 del texto normalizado del bloque.
    huella: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    veces_visto: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    medios_distintos: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    primera_vez: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ultima_vez: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Recorte legible para que un humano pueda auditar por que se esta
    # saltando este bloque -- sin esto, un falso positivo (una noticia
    # recurrente marcada como publicidad) seria invisible.
    muestra_texto: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL = todavia no revisado por un humano. True/False = confirmado.
    # El filtro automatico usa veces_visto; esta columna permite forzar
    # (o desmentir) la decision manualmente.
    es_publicidad: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
