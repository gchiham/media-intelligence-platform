"""Puerto abstracto para transcripcion de audio -- mismo patron que
AIAnalysisProvider (src/modules/ai/providers/base.py). Cualquier motor
(Faster Whisper, Whisper API, WhisperX, otros) se conecta implementando esta
interfaz, sin que el resto de la aplicacion sepa cual es."""
from abc import ABC, abstractmethod
from pathlib import Path

from src.modules.transcription.models.transcription_models import TranscriptionResult


class TranscriptionProvider(ABC):
    @abstractmethod
    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        raise NotImplementedError
