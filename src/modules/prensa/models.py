"""Prensa digital: la pata escrita del monitoreo, complementaria a la de audio.

La captura de radio/TV es push (mediaCAP deja archivos en S3 y DiscoveryService
los levanta). La web es al reves: nadie avisa cuando se publica una nota, hay
que ir a preguntar. Ningun feed hondureno anuncia hub WebSub, asi que el unico
mecanismo posible es polling por cron -- ver scripts/rss_ingest.py.

Un `Articulo` NO es una `Noticia` del dominio Editorial: `Noticia` es la unidad
que produce nuestro pipeline a partir de una grabacion (tiene grabacion_id y
offsets de clip obligatorios) y que el periodista aprueba. Un `Articulo` es
material crudo publicado por un tercero. Mapear articulos a noticias implica
hacer `Noticia.grabacion_id` nullable; queda fuera de alcance por ahora.
"""
import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TipoFuente(str, enum.Enum):
    RSS = "rss"
    # Sitemap de Google News (<news:publication_date>, <news:title>). Lo usan los
    # medios de Grupo OPSA, que corren Liferay y no exponen RSS por ninguna ruta.
    SITEMAP_NEWS = "sitemap_news"


class FuenteWeb(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Un endpoint que se pollea. Global (no por tenant), igual que Medio: la
    captura es infraestructura compartida entre clientes.

    Un Medio puede tener varias fuentes (portada + secciones, o feed + sitemap),
    por eso la unicidad esta en la url y no en medio_id.
    """

    __tablename__ = "fuentes_web"

    medio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medios.id"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    tipo: Mapped[TipoFuente] = mapped_column(
        Enum(TipoFuente, name="tipo_fuente", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    # Fecha de corte: no ingerir nada publicado antes. Sin esto, el primer poll de
    # una fuente se traga todo el historial que traiga el feed -- Televicentro
    # arrastra 625 h y ContraCorriente 546 h en 9 y 15 items. Mismo criterio que
    # se uso al dar de alta canal_10 (commit 4c80416).
    fecha_corte: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Validadores HTTP para el GET condicional. Se guardan crudos, tal como los
    # manda el servidor, porque hay que devolverlos textuales en If-None-Match /
    # If-Modified-Since. De las 10 fuentes que se midieron el 2026-08-19, 8
    # contestan 304 sin cuerpo; proceso.hn y televicentro.hn no mandan ningun
    # validador, y los sitemaps de OPSA tampoco (Cache-Control: max-age=1).
    # Esas quedan en NULL para siempre y bajan el cuerpo completo cada vez.
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)

    ultimo_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Texto corto para diagnostico desde SQL sin leer logs: "ok: 3 nuevos",
    # "sin_cambios", "error: HTTP 403".
    ultimo_resultado: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Se resetea a 0 en cada pasada exitosa. Sirve para detectar la fuente que
    # se rompio en silencio: un feed que cambio de URL devuelve 404 para
    # siempre y nadie se entera si solo se mira que el cron "corrio bien".
    errores_consecutivos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Articulo(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Una nota publicada en la web de un medio, tal como vino en el feed."""

    __tablename__ = "articulos"
    __table_args__ = (
        # La clave de dedup. El cron ve los mismos 10 items ~96 veces por dia:
        # sin esto, una pasada cada 15 min genera 960 filas duplicadas diarias
        # por fuente. El guid es por fuente, no global, porque nada garantiza
        # que dos medios distintos no usen el mismo string.
        UniqueConstraint("fuente_id", "guid", name="uq_articulos_fuente_guid"),
    )

    fuente_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fuentes_web.id"), nullable=False, index=True
    )
    # <guid> del RSS, o el <loc> del sitemap. Se cae al link cuando el feed no
    # trae guid (lo permite la spec de RSS 2.0).
    guid: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    # Los tres campos de texto vienen con HTML crudo del feed: limpiarlo es
    # trabajo del pipeline que consuma esto, no de la ingesta.
    resumen: Mapped[str | None] = mapped_column(Text, nullable=True)
    # <content:encoded>. Lo traen 9 de las 17 fuentes RSS; en las otras hay que
    # ir a buscar el cuerpo a la nota si se lo necesita completo.
    contenido_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    autor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Portada del articulo: media:content/media:thumbnail o <enclosure> en RSS,
    # <image:image> en el sitemap de Google News, o el primer <img> del cuerpo
    # como ultimo recurso (ver feeds._imagen_rss). Nunca se descarga la imagen
    # en si, solo se guarda la URL -- igual que clip_s3_uri en Noticia, servir
    # el binario es responsabilidad de quien consuma esto.
    imagen_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    publicado_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# Prensa impresa (PDF). Comparte modulo con la web porque las dos son prensa,
# pero NO comparte tablas: el articulo de RSS y la nota de periodico son cosas
# distintas y forzarlas en `articulos` obligaba a dejar en nullable la mitad de
# las columnas y a un "exactamente uno de fuente_id o edicion_id" -- la senal
# clasica de dos entidades apretadas en una. Para los reportes que quieren ver
# ambas juntas, la union se hace en la consulta, no en el esquema.
# ---------------------------------------------------------------------------


class Edicion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Una edicion impresa completa, tal como la publico el canal de Telegram.

    Es tabla propia y no columnas en la nota porque el PDF, sus paginas
    renderizadas y el costo de extraerlo son de la EDICION, no de cada nota:
    al re-procesar con otro modelo lo que cambia es la edicion entera.
    """

    __tablename__ = "ediciones"
    __table_args__ = (
        # Dedup por contenido, no por nombre. El canal republica el mismo PDF:
        # medido sobre agosto 2026, 2 de 68 archivos (3%) eran byte-identicos a
        # otro, y un par cruzaba de carpeta de fecha (Tiempo del 01-ago
        # reposteado el 02). Sin esto se paga la extraccion dos veces y la nota
        # sale duplicada en el portal.
        UniqueConstraint("sha256", name="uq_ediciones_sha256"),
    )

    medio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medios.id"), nullable=False, index=True
    )
    # La del periodico, NO la de publicacion en Telegram: difieren. Un diario
    # del 16-ago puede subirse el 17; agrupar por la fecha de Telegram inventa
    # dias sin edicion y duplica otros.
    fecha_edicion: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    s3_pdf_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    paginas_total: Mapped[int] = mapped_column(Integer, nullable=False)
    # Paginas sin texto extraible (deportes, planas de foto): hay que mandarlas
    # al modelo como imagen. De 28 paginas de Diario Tiempo, 14 no tienen una
    # sola palabra recuperable con pdfplumber.
    paginas_sin_texto: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    # Mensaje de origen (https://t.me/<canal>/<id>).
    origen_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Procedencia de la extraccion: sin esto no se puede re-correr
    # selectivamente cuando cambie el modelo o el prompt.
    modelo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extraido_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_entrada: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_salida: Mapped[int | None] = mapped_column(Integer, nullable=True)


class NotaImpresa(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Una nota periodistica dentro de una edicion impresa."""

    __tablename__ = "notas_impresas"
    __table_args__ = (
        # Idempotencia al re-extraer: el indice es el orden en que el modelo
        # devolvio la nota dentro de la edicion.
        UniqueConstraint("edicion_id", "indice", name="uq_notas_impresas_edicion_indice"),
    )

    edicion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ediciones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    indice: Mapped[int] = mapped_column(Integer, nullable=False)
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    sumario: Mapped[str | None] = mapped_column(Text, nullable=True)
    cuerpo: Mapped[str] = mapped_column(Text, nullable=False)
    seccion: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Paginas donde sale la nota, en orden. Array y no entero porque una nota
    # que dice "pasa a la pag. 12" vive en dos, y el front tiene que poder
    # abrir las dos. Medido: el modelo devolvio pagina en el 100% de las notas.
    paginas: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)


class NotaTraduccion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Traduccion de una `NotaImpresa`.

    Tabla aparte y no columnas `titulo_en`/`cuerpo_en` por dos razones de
    operacion: hay que saber QUE modelo produjo cada traduccion (para rehacerla
    selectivamente cuando mejore, igual que con la extraccion), y la traduccion
    se reintenta por separado -- con columnas, cada reintento ensuciaria el
    `updated_at` de la nota.
    """

    __tablename__ = "nota_traducciones"
    __table_args__ = (
        UniqueConstraint("nota_id", "idioma", name="uq_nota_traduccion_idioma"),
    )

    nota_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notas_impresas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idioma: Mapped[str] = mapped_column(String(8), nullable=False)  # ISO 639-1
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    sumario: Mapped[str | None] = mapped_column(Text, nullable=True)
    cuerpo: Mapped[str | None] = mapped_column(Text, nullable=True)
    modelo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    traducido_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
