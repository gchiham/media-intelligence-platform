"""Adaptador que devuelve segmentos ya calculados en vez de llamar al LLM.

Es la pieza que deja usar la Batch API sin tocar nada del pipeline: el
orquestador, el mapeo a tiempo, el clipping, el versionado de Noticia y la
idempotencia de PipelineRunService siguen exactamente igual, porque desde su
punto de vista esto es un `AIAnalysisProvider` como cualquier otro -- solo que
la "inferencia" ya ocurrio horas antes, dentro de un batch al 50% de costo.

Ver src/modules/ai/batch.py para como se llenan esos segmentos.
"""
from src.modules.ai.providers.base import AIAnalysisProvider
from src.modules.ai.schemas import NewsSegment, Word


class PrecomputedAnalysisProvider(AIAnalysisProvider):
    def __init__(self, segments: list[NewsSegment]):
        self._segments = segments

    def segment_news(self, words: list[Word]) -> list[NewsSegment]:
        # `words` se ignora a proposito: los segmentos ya fueron calculados
        # sobre exactamente estas palabras (la cache se llena por grabacion_id,
        # y el words.json de una grabacion es inmutable una vez transcrita).
        return self._segments
