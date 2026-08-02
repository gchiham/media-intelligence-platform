"""Tablas del portal de validacion de Destiller (docs/DESTILLER_PROPOSAL.md).

Existen para que varias personas validen a la vez desde el servidor de MIP, en
vez de una sola con un HTML local y `localStorage`. Eso obliga a dos cosas que
el archivo local no necesitaba: un lugar compartido donde vivan los veredictos,
y un mecanismo para que dos validadores no revisen el mismo bloque.

**No son tablas de produccion del pipeline.** Guardan el set de evaluacion y su
validacion humana -- el insumo para elegir el umbral de Destiller. Cuando el
umbral quede fijado, esto sigue sirviendo como regresion: se vuelve a correr el
mismo set contra un modelo nuevo y se compara contra los mismos veredictos.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TipoBloqueDB(str, enum.Enum):
    """Espejo de TipoBloque en src/modules/ai/destiller.py. Se duplica a
    proposito: el enum de la base es un contrato de datos que no puede cambiar
    silenciosamente porque alguien agrego un tipo en el codigo del modelo."""

    NOTICIA = "noticia"
    PUBLICIDAD = "publicidad"
    RELIGIOSO = "religioso"
    PROMO = "promo"
    MUSICA = "musica"
    RELLENO = "relleno"
    DESCONOCIDO = "desconocido"


_ENUM = Enum(TipoBloqueDB, name="tipo_bloque_destiller", values_callable=lambda x: [e.value for e in x])


class BloqueValidacion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Un bloque propuesto por un modelo, pendiente de validar o ya validado."""

    __tablename__ = "destiller_bloques"
    __table_args__ = (
        # Un mismo bloque de una misma grabacion puede existir una vez por
        # modelo evaluado -- asi conviven las particiones de Qwen y de
        # gpt-4o-mini sobre las mismas grabaciones sin pisarse.
        UniqueConstraint("modelo", "grabacion_ref", "bloque_idx", name="uq_destiller_bloque"),
    )

    modelo: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    grabacion_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    medio: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    fecha: Mapped[str] = mapped_column(String(10), nullable=False)
    hora_local: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    bloque_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    start_word: Mapped[int] = mapped_column(Integer, nullable=False)
    end_word: Mapped[int] = mapped_column(Integer, nullable=False)
    inicio_seg: Mapped[float] = mapped_column(Float, nullable=False)
    fin_seg: Mapped[float] = mapped_column(Float, nullable=False)
    tipo_llm: Mapped[TipoBloqueDB] = mapped_column(_ENUM, nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    motivo: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    texto: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Priorizacion de la cola -------------------------------------------
    # La validacion humana empieza por donde los modelos discrepan, que es
    # donde esta la informacion. El numero ES el orden de entrega, no una
    # etiqueta: la cola ordena por esta columna.
    #   1  mayoria de palabras discrepan en la decision binaria (basura/no)
    #   2  minoria discrepan en binario -- los modelos cortan distinto
    #   3  muestra de control: acuerdo exacto, 20% deterministico
    #   4  acuerdo binario, desacuerdo de subtipo
    #   5  resto de los acuerdos exactos
    # El control va antes que el subtipo a proposito: en esta etapa lo que se
    # valida es la decision binaria, que es la unica que el pipeline usa --
    # que un anuncio sea `promo` o `publicidad` no cambia que se excluya.
    prioridad: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    # Fraccion de palabras del bloque que el otro modelo pone del lado
    # contrario (0-1). Se guarda para poder reordenar sin recalcular, y para
    # auditar por que un bloque quedo donde quedo.
    desacuerdo_binario: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Fraccion de palabras con el tipo exactamente igual (0-1).
    porcentaje_acuerdo: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # Trazabilidad: de que comparacion salio esta prioridad. Cabe UNA sola --
    # una segunda comparacion (GPT-5, Claude) sobrescribe estos campos. Para
    # acumular varias hace falta una tabla hija; se agrega cuando exista el
    # segundo comparado, no antes.
    modelo_base: Mapped[str | None] = mapped_column(String(120), nullable=True)
    modelo_comparado: Mapped[str | None] = mapped_column(String(120), nullable=True)
    version_comparacion: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Bloqueo por validador, mismo patron que `Noticia.asignado_a` (FR-051):
    # mientras alguien tiene el bloque asignado, no se le entrega a nadie mas.
    # Es un lease con vencimiento, no un candado permanente -- si el validador
    # cierra el navegador a media tanda, el bloque tiene que volver a la cola
    # sola, sin intervencion manual.
    asignado_a: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    asignado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VeredictoValidacion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """El juicio humano sobre un bloque.

    Se guarda `tipo_llm` copiado aunque ya este en el bloque: la calibracion
    compara lo que dijo el modelo contra lo que era en realidad, y si algun dia
    se recarga el bloque con otra corrida, el veredicto tiene que seguir
    diciendo sobre que afirmacion se pronuncio el humano.
    """

    __tablename__ = "destiller_veredictos"
    __table_args__ = (
        # Un validador opina una vez por bloque. Que dos personas distintas
        # opinen sobre el mismo bloque si esta permitido y es deseable: es la
        # unica forma de medir el acuerdo entre validadores, o sea que tan
        # confiable es la verdad que estamos construyendo.
        UniqueConstraint("bloque_id", "validador", name="uq_destiller_veredicto"),
    )

    bloque_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("destiller_bloques.id", ondelete="CASCADE"), nullable=False, index=True
    )
    validador: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tipo_llm: Mapped[TipoBloqueDB] = mapped_column(_ENUM, nullable=False)
    # Solo cuando ok=False: que era en realidad.
    tipo_real: Mapped[TipoBloqueDB | None] = mapped_column(_ENUM, nullable=True)
