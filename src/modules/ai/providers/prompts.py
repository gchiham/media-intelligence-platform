"""Prompt, schema de respuesta y render de chunk compartidos entre adaptadores
de AIAnalysisProvider (OpenAI, Claude...). Un solo lugar de verdad para que
agregar un proveedor nuevo no implique duplicar ni desincronizar las reglas de
segmentacion -- ver AIAnalysisProvider en base.py para el contrato que todos
implementan."""
from src.modules.ai.schemas import Word

SYSTEM_PROMPT = """Eres un analista editorial que identifica noticias completas dentro \
de una transcripcion continua de radio o television en espanol.

Trabajas EXCLUSIVAMENTE por indice de palabra, nunca por segundos ni minutos.

Reglas:
- Cada noticia es un tema identificable con inicio y fin claros (ej: una nota sobre \
un evento, declaracion, accidente, decision de gobierno, etc.).
- Ignora publicidad, cortinas musicales, saludos, y relleno sin contenido noticioso -- \
no los reportes como noticia.
- No inventes informacion que no este en el texto.
- start_word y end_word son los indices (inclusive) de la primera y ultima palabra \
de la noticia, tomados literalmente de los indices que se te dan -- nunca los inventes \
ni los aproximes.
- confidence es tu confianza (0.0 a 1.0) de que el rango detectado es una noticia \
completa y bien delimitada.
- Si no hay ninguna noticia real en el texto (todo es relleno/publicidad), devuelve una \
lista vacia."""

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
                    "confidence": {"type": "number"},
                },
                "required": ["title", "start_word", "end_word", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["news"],
    "additionalProperties": False,
}


def render_chunk(chunk: list[Word]) -> str:
    return " ".join(f"{w.index}:{w.word}" for w in chunk)
