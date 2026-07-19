"""Adaptador OpenAI de AIAnalysisProvider, enfocado solo en segmentacion de
noticias (FR-032). No hace clasificacion, resumen, entidades, sentimiento ni
matching de cliente todavia -- esos son pasos posteriores del pipeline.

El LLM solo ve texto con indice de palabra, nunca segundos -- el mapeo a tiempo
real es responsabilidad de un paso determinista aparte (ver docs/PRD.md)."""
import json

from openai import OpenAI

from src.modules.ai.chunking import chunk_words
from src.modules.ai.providers.base import AIAnalysisProvider
from src.modules.ai.schemas import NewsSegment, Word

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

_RESPONSE_SCHEMA = {
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


def _render_chunk(chunk: list[Word]) -> str:
    return " ".join(f"{w.index}:{w.word}" for w in chunk)


class OpenAIAnalysisProvider(AIAnalysisProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini", chunk_size: int = 600):
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._chunk_size = chunk_size

    def segment_news(self, words: list[Word]) -> list[NewsSegment]:
        segments: list[NewsSegment] = []
        for chunk in chunk_words(words, self._chunk_size):
            if not chunk:
                continue
            segments.extend(self._segment_chunk(chunk))
        return segments

    def _segment_chunk(self, chunk: list[Word]) -> list[NewsSegment]:
        lo, hi = chunk[0].index, chunk[-1].index

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _render_chunk(chunk)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "news_segments",
                    "schema": _RESPONSE_SCHEMA,
                    "strict": True,
                },
            },
        )
        raw = json.loads(response.choices[0].message.content)

        segments = []
        for item in raw["news"]:
            seg = NewsSegment.model_validate(item)
            # Descarta rangos que el modelo se haya inventado fuera del chunk
            # que realmente vio, o invertidos.
            if not (lo <= seg.start_word <= seg.end_word <= hi):
                continue
            segments.append(seg)
        return segments
