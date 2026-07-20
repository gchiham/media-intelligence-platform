"""Compone dos AIAnalysisProvider con fallback completo (nunca por chunk):
si el primario agota sus propios reintentos y falla, se reintenta la
segmentacion entera con el secundario. Ver AIAnalysisProvider en base.py para
el contrato -- este wrapper tambien lo implementa, asi que
MediaProcessingOrchestrator no distingue un proveedor simple de uno con
fallback."""
import logging

from src.modules.ai.exceptions import SegmentationError
from src.modules.ai.providers.base import AIAnalysisProvider
from src.modules.ai.schemas import NewsSegment, Word

logger = logging.getLogger(__name__)


class AIProviderWithFallback(AIAnalysisProvider):
    def __init__(self, primary: AIAnalysisProvider, secondary: AIAnalysisProvider):
        self._primary = primary
        self._secondary = secondary

    def segment_news(self, words: list[Word]) -> list[NewsSegment]:
        try:
            return self._primary.segment_news(words)
        except SegmentationError as primary_error:
            logger.warning("proveedor primario de IA fallo, reintentando con el secundario: %s", primary_error)
            try:
                return self._secondary.segment_news(words)
            except SegmentationError as secondary_error:
                raise SegmentationError(
                    f"fallaron ambos proveedores de IA -- primario: {primary_error}; secundario: {secondary_error}"
                ) from secondary_error
