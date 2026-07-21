"""Adaptador Claude (Anthropic) de AIAnalysisProvider -- mismo contrato y mismo
enfoque que OpenAIAnalysisProvider (ver ese archivo para el detalle del
diseno): el LLM solo ve indice de palabra, nunca segundos.

No hay equivalente exacto al `response_format` json_schema strict de OpenAI en
la API de Claude -- en su lugar se fuerza una tool call con `strict: True` y
`tool_choice` fijo a esa tool, que da la misma garantia de forma de salida
(ver skill claude-api, seccion "Structured Outputs" / "Strict tool use")."""
import time

import anthropic
from anthropic import Anthropic

from src.modules.ai.chunking import chunk_words
from src.modules.ai.exceptions import SegmentationError
from src.modules.ai.providers.base import AIAnalysisProvider
from src.modules.ai.providers.prompts import MAX_KEYWORDS, RESPONSE_SCHEMA, SYSTEM_PROMPT, render_chunk
from src.modules.ai.schemas import NewsSegment, Word
from src.shared.errors import PermanentPipelineError, TransientPipelineError, classify_and_wrap

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [1, 2]  # espera antes del intento 2 y del intento 3

_TOOL_NAME = "return_news_segments"


class AnthropicAnalysisProvider(AIAnalysisProvider):
    def __init__(self, api_key: str, model: str = "claude-sonnet-5", chunk_size: int = 600):
        self._client = Anthropic(api_key=api_key)
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
                response = self._client.messages.create(
                    model=self._model,
                    # 4096 alcanzaba con el schema viejo (solo title/rango/confidence),
                    # pero un chunk denso en titulares (varias decenas de noticias
                    # cortas en 600 palabras, ej. un resumen de "titulares") puede
                    # superar 4096 tokens de salida con summary+keywords+entidades
                    # por item -- Claude corta el JSON a medias y la tool call
                    # queda invalida. Visto en produccion al ampliar el schema.
                    max_tokens=8192,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": render_chunk(chunk)}],
                    tools=[
                        {
                            "name": _TOOL_NAME,
                            "description": "Devuelve las noticias detectadas en el chunk.",
                            "input_schema": RESPONSE_SCHEMA,
                            "strict": True,
                        }
                    ],
                    tool_choice={"type": "tool", "name": _TOOL_NAME},
                )
                return self._extract_tool_input(response)
            except Exception as exc:
                error = classify_and_wrap(exc, module="segmentation")
                if isinstance(error, PermanentPipelineError):
                    raise SegmentationError(str(error)) from error
                last_error = error
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_BACKOFF_SECONDS[attempt - 1])

        raise SegmentationError(
            f"agotados {_MAX_ATTEMPTS} intentos contra Claude: {last_error}"
        ) from last_error

    @staticmethod
    def _extract_tool_input(response: anthropic.types.Message) -> dict:
        for block in response.content:
            if block.type == "tool_use" and block.name == _TOOL_NAME:
                if "news" not in block.input:
                    # Tool call presente pero incompleta -- normalmente porque
                    # se corto la generacion (stop_reason=max_tokens) antes de
                    # cerrar el JSON. Se trata como transitorio: classify_and_wrap
                    # clasifica un ValueError comun como TransientPipelineError
                    # por default, asi que esto reintenta en vez de tumbar
                    # segment_news entero con un KeyError sin capturar.
                    raise ValueError(
                        f"tool call sin 'news' (stop_reason={response.stop_reason}) -- "
                        "probable corte por max_tokens"
                    )
                return block.input
        raise SegmentationError("Claude no devolvio la tool call esperada")
