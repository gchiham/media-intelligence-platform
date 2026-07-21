"""Adaptador OpenAI de AIAnalysisProvider, enfocado solo en segmentacion de
noticias (FR-032). No hace clasificacion, resumen, entidades, sentimiento ni
matching de cliente todavia -- esos son pasos posteriores del pipeline.

El LLM solo ve texto con indice de palabra, nunca segundos -- el mapeo a tiempo
real es responsabilidad de un paso determinista aparte (ver docs/PRD.md).

Reintentos (R3 de docs/ARCHITECTURE_REVIEW.md): un rate limit o un blip de red
no debe tumbar toda la segmentacion. `classify_and_wrap` (ya usado en el
manejo de errores del worker de transcripcion, ver docs/ERROR_HANDLING.md)
distingue error transitorio (reintentable, ej. 429/timeout) de permanente
(ej. API key invalida) -- aqui el backoff es corto (segundos, no minutos)
porque corre sincronico dentro de un request HTTP (POST /pipeline/process),
a diferencia del backoff del worker SQS que puede esperar minutos."""
import json
import time

from openai import OpenAI

from src.modules.ai.chunking import chunk_words
from src.modules.ai.exceptions import SegmentationError
from src.modules.ai.providers.base import AIAnalysisProvider
from src.modules.ai.providers.prompts import MAX_KEYWORDS
from src.modules.ai.providers.prompts import RESPONSE_SCHEMA as _RESPONSE_SCHEMA
from src.modules.ai.providers.prompts import SYSTEM_PROMPT
from src.modules.ai.providers.prompts import render_chunk as _render_chunk
from src.modules.ai.schemas import NewsSegment, Word
from src.shared.errors import PermanentPipelineError, TransientPipelineError, classify_and_wrap

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [1, 2]  # espera antes del intento 2 y del intento 3


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
        raw = self._call_with_retry(chunk)

        segments = []
        for item in raw["news"]:
            item["keywords"] = item.get("keywords", [])[:MAX_KEYWORDS]
            seg = NewsSegment.model_validate(item)
            # Descarta rangos que el modelo se haya inventado fuera del chunk
            # que realmente vio, o invertidos.
            if not (lo <= seg.start_word <= seg.end_word <= hi):
                continue
            segments.append(seg)
        return segments

    def _call_with_retry(self, chunk: list[Word]) -> dict:
        last_error: TransientPipelineError | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
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
                return json.loads(response.choices[0].message.content)
            except Exception as exc:
                error = classify_and_wrap(exc, module="segmentation")
                if isinstance(error, PermanentPipelineError):
                    raise SegmentationError(str(error)) from error
                last_error = error
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_BACKOFF_SECONDS[attempt - 1])

        raise SegmentationError(
            f"agotados {_MAX_ATTEMPTS} intentos contra OpenAI: {last_error}"
        ) from last_error
