"""Puerto abstracto (Ports & Adapters, ver docs/PRD.md) para el analisis semantico
de IA. FR-041: el analisis nunca se ata a un solo proveedor de LLM."""
from abc import ABC, abstractmethod

from src.modules.ai.schemas import NewsSegment, Word


class AIAnalysisProvider(ABC):
    @abstractmethod
    def segment_news(self, words: list[Word]) -> list[NewsSegment]:
        """Identifica noticias completas dentro de una transcripcion continua,
        delimitadas por indice de palabra (inclusive en ambos extremos)."""
        raise NotImplementedError
