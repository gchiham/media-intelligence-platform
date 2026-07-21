"""Tipos de datos puros del modulo AI -- sin dependencia de SQLAlchemy ni de un
proveedor concreto.

`Word` NO se redefine aqui -- la definicion oficial y unica del contrato vive
en src/modules/transcription/models/transcription_models.py (lo que produce
FasterWhisperProvider). Se re-exporta por conveniencia para que el resto del
modulo AI pueda seguir escribiendo `from src.modules.ai.schemas import Word`."""
import enum

from pydantic import BaseModel, Field

from src.modules.transcription.models.transcription_models import Word

__all__ = ["Word", "NewsType", "NewsSegment"]


class NewsType(str, enum.Enum):
    """Categoria editorial de la noticia. Enum cerrado a proposito -- texto
    libre deriva en variantes ("Politica"/"politica"/"Politico") que despues
    no se pueden agrupar ni filtrar de forma confiable."""

    POLITICA = "politica"
    ECONOMIA = "economia"
    DEPORTES = "deportes"
    SALUD = "salud"
    SEGURIDAD = "seguridad"
    SOCIEDAD = "sociedad"
    INTERNACIONAL = "internacional"
    TECNOLOGIA = "tecnologia"
    ENTRETENIMIENTO = "entretenimiento"
    OTRO = "otro"


class NewsSegment(BaseModel):
    """Una noticia propuesta por el LLM, delimitada por indice de palabra (no por
    segundos -- el mapeo a tiempo real es un paso determinista posterior, ver
    docs/PRD.md y el diseno de github.com/gchiham/mvp-medios).

    El LLM solo ve texto por indice de palabra -- nunca sabe de que Grabacion
    viene el chunk, asi que no puede inferir (ni se le pide) programa,
    periodista, emisora, fecha ni hora. Esos campos salen siempre de la
    metadata del sistema (Grabacion/Programa/Medio), nunca de este modelo.

    people/organizations/locations son menciones crudas extraidas del texto,
    sin resolver contra el catalogo de Entidad -- esa deduplicacion/matching
    (¿"JOH" y "Juan Orlando Hernandez" son la misma persona?) es un paso
    posterior, fuera de alcance de la segmentacion."""

    title: str
    start_word: int
    end_word: int
    summary: str
    keywords: list[str]
    news_type: NewsType
    people: list[str]
    organizations: list[str]
    locations: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
