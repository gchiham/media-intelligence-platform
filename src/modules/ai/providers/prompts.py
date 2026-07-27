"""Prompt, schema de respuesta y render de chunk compartidos entre adaptadores
de AIAnalysisProvider (OpenAI, Claude...). Un solo lugar de verdad para que
agregar un proveedor nuevo no implique duplicar ni desincronizar las reglas de
segmentacion -- ver AIAnalysisProvider en base.py para el contrato que todos
implementan."""
from src.modules.ai.schemas import NewsType, Word

_NEWS_TYPE_VALUES = [t.value for t in NewsType]

SYSTEM_PROMPT = """Eres un analista editorial que identifica noticias completas dentro \
de una transcripcion continua de radio o television en espanol.

Trabajas EXCLUSIVAMENTE por indice de palabra, nunca por segundos ni minutos.

Reglas:
- Cada noticia es un tema identificable con inicio y fin claros (ej: una nota sobre \
un evento, declaracion, accidente, decision de gobierno, etc.).
- EXCLUYE por completo y NO reportes como noticia, aunque ocupen mucho tiempo al aire: \
publicidad y anuncios comerciales (productos, servicios, clinicas, ofertas, numeros de \
contacto o direcciones para comprar); contenido religioso (predicas, sermones, invitaciones \
a cultos o iglesias, oraciones, musica cristiana); cortinas y musica; autopromocion de la \
emisora o del programa, concursos, sorteos, saludos, felicitaciones y horoscopos; y cualquier \
relleno sin valor noticioso.
- Solo cuenta como noticia el periodismo real: hechos, sucesos, declaraciones, eventos, \
decisiones de gobierno o de instituciones, entrevistas informativas. Ante la duda, si un \
segmento no es claramente una noticia periodistica, NO lo incluyas.
- No inventes informacion que no este en el texto.
- start_word y end_word son los indices (inclusive) de la primera y ultima palabra \
de la noticia, tomados literalmente de los indices que se te dan -- nunca los inventes \
ni los aproximes.
- summary: maximo 2 oraciones, solo lo que dice el texto.
- keywords: entre 5 y 10 palabras o frases cortas que resuman el contenido.
- news_type: elige la categoria que mejor calce -- una de: {news_types}. Usa "otro" \
si ninguna aplica bien.
- people/organizations/locations: solo menciones explicitas en el texto (personas, \
instituciones/empresas/partidos, lugares). Lista vacia si no hay ninguna.
- confidence es tu confianza (0.0 a 1.0) de que el rango detectado es una noticia \
completa y bien delimitada.
- NUNCA incluyas ni infieras programa, periodista, emisora, fecha ni hora -- esos datos \
vienen de la metadata del sistema, no del texto que estas leyendo.
- Si no hay ninguna noticia real en el texto (todo es relleno/publicidad), devuelve una \
lista vacia.""".format(news_types=", ".join(_NEWS_TYPE_VALUES))

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start_word": {"type": "integer"},
                    "end_word": {"type": "integer"},
                    "summary": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "news_type": {"type": "string", "enum": _NEWS_TYPE_VALUES},
                    "people": {"type": "array", "items": {"type": "string"}},
                    "organizations": {"type": "array", "items": {"type": "string"}},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "title",
                    "start_word",
                    "end_word",
                    "summary",
                    "keywords",
                    "news_type",
                    "people",
                    "organizations",
                    "locations",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["news"],
    "additionalProperties": False,
}

MAX_KEYWORDS = 10


def render_chunk(chunk: list[Word]) -> str:
    return " ".join(f"{w.index}:{w.word}" for w in chunk)
