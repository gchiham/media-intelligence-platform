"""Adaptador Faster Whisper de TranscriptionProvider.

Los defaults de este constructor SON la configuracion de produccion validada
en docs/OPTIMIZATION_REPORT.md (Whisper Small, int8_float16, batch_size=24,
VAD activo, word timestamps activos) -- este archivo no cambia ese
comportamiento, solo lo envuelve detras de la interfaz para que sea
reutilizable e intercambiable.

El import de `faster_whisper` es perezoso (dentro de __init__, no a nivel de
modulo) para que el resto de la aplicacion pueda importar este archivo, o
TranscriptionProvider en general, sin necesitar torch/CUDA instalado -- eso
solo hace falta donde efectivamente se instancia FasterWhisperProvider
(chepita, la instancia GPU)."""
from pathlib import Path

from src.modules.transcription.models.transcription_models import (
    TranscriptionResult,
    TranscriptionSegment,
    Word,
)
from src.modules.transcription.providers.transcription_provider import TranscriptionProvider


class FasterWhisperProvider(TranscriptionProvider):
    def __init__(
        self,
        model_name: str = "small",
        compute_type: str = "int8_float16",
        batch_size: int = 24,
        language: str = "es",
        vad_filter: bool = True,
        device: str = "cuda",
    ):
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        self._batch_size = batch_size
        self._language = language
        self._vad_filter = vad_filter

        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._pipeline = BatchedInferencePipeline(model=model)

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        segments_iter, info = self._pipeline.transcribe(
            str(audio_path),
            language=self._language,
            batch_size=self._batch_size,
            vad_filter=self._vad_filter,
            word_timestamps=True,
        )
        segments_list = list(segments_iter)

        segments = [
            TranscriptionSegment(start=seg.start, end=seg.end, text=seg.text)
            for seg in segments_list
        ]
        words = [
            Word(index=idx, word=w.word.strip(), start=round(w.start, 2), end=round(w.end, 2))
            for idx, w in enumerate(w for seg in segments_list for w in (seg.words or []))
        ]

        return TranscriptionResult(
            language=info.language,
            duration=info.duration,
            segments=segments,
            words=words,
        )
