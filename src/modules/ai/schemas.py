"""Tipos de datos puros del modulo AI -- sin dependencia de SQLAlchemy ni de un
proveedor concreto.

`Word` NO se redefine aqui -- la definicion oficial y unica del contrato vive
en src/modules/transcription/models/transcription_models.py (lo que produce
FasterWhisperProvider). Se re-exporta por conveniencia para que el resto del
modulo AI pueda seguir escribiendo `from src.modules.ai.schemas import Word`."""
import enum

from pydantic import BaseModel, Field

from src.modules.transcription.models.transcription_models import Word

__all__ = ["Word", "NewsType", "NewsSegment", "PerfilCliente", "MatchedOn", "ClientRelevance"]


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


class MatchedOn(str, enum.Enum):
    """Por que via una noticia resulto relevante para un cliente. `CONTENIDO`
    es la que justifica usar un LLM en vez de un grep: la noticia le importa
    al cliente aunque no lo nombre (una decision que lo afecta, un proceso en
    el que es parte, una institucion que preside)."""

    PERSONA = "persona"
    INSTITUCION = "institucion"
    TEMA = "tema"
    CONTENIDO = "contenido"


class ClientRelevance(BaseModel):
    """Veredicto de relevancia de UNA noticia para UN cliente."""

    client_id: str
    relevant: bool
    score: float = Field(ge=0.0, le=1.0)
    rationale: str
    matched_on: list[MatchedOn]


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

    # Solo lo llena el batch de OpenAI, que es el unico camino que manda los
    # perfiles de cliente en la llamada; el sincronico (Claude) lo deja vacio.
    # Default vacio y no opcional para que leer una Noticia vieja de
    # `segmentation_cache` (cacheada antes de que esto existiera) siga
    # validando sin migrar nada.
    client_relevance: list[ClientRelevance] = Field(default_factory=list)


class PerfilCliente(BaseModel):
    """Lo que un cliente quiere que se le monitoree, en la forma minima que
    necesita el LLM.

    Es la proyeccion de un `MonitoringProfile` (una fila por tenant, ver
    src/modules/editorial/models.py) hacia el modulo AI, que no debe conocer
    SQLAlchemy. Agregar un cliente es insertar una fila en esa tabla -- ni el
    prompt ni el esquema de respuesta cambian, por eso `client_id` viaja como
    string y no como enum."""

    client_id: str
    nombre: str
    personas_interes: list[str] = Field(default_factory=list)
    instituciones: list[str] = Field(default_factory=list)
    temas: list[str] = Field(default_factory=list)
