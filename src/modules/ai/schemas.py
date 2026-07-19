"""Tipos de datos puros del modulo AI -- sin dependencia de SQLAlchemy ni de un
provider concreto.

`Word` NO se redefine aqui -- la definicion oficial y unica del contrato vive
en src/modules/transcription/models/transcription_models.py (lo que produce
FasterWhisperProvider). Se re-exporta por conveniencia para que el resto del
modulo AI pueda seguir escribiendo `from src.modules.ai.schemas import Word`."""
from pydantic import BaseModel, Field

from src.modules.transcription.models.transcription_models import Word

__all__ = ["Word", "NewsSegment"]


class NewsSegment(BaseModel):
    """Una noticia propuesta por el LLM, delimitada por indice de palabra (no por
    segundos -- el mapeo a tiempo real es un paso determinista posterior, ver
    docs/PRD.md y el diseno de github.com/gchiham/mvp-medios)."""

    title: str
    start_word: int
    end_word: int
    confidence: float = Field(ge=0.0, le=1.0)
