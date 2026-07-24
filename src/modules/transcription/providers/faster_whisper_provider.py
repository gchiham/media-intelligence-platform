"""Adaptador Faster Whisper de TranscriptionProvider.

Los defaults de este constructor SON la configuracion de produccion (ver
docs/OPTIMIZATION_REPORT.md para el tuning de batch/prefetch, que sigue
vigente) -- este archivo no agrega logica propia, solo envuelve la libreria
detras de la interfaz para que sea reutilizable e intercambiable.

**Modelo: `large-v3-turbo`, no `small`** (cambiado 2026-07-22, ver
docs/EFFICIENCY_REVIEW.md §5). El motivo no es velocidad sino *calidad de
nombres propios*: en monitoreo de medios el nombre propio es el producto --
si dice "Siomara Castro" la busqueda por entidad del cliente no encuentra la
mencion. turbo son 809M parametros contra 244M de small, retiene >95% de la
exactitud de large-v3, y en int8 ocupa ~1.6 GB de VRAM: la L4 tiene 23 GB y
la config de 6 workers usaba ~7 GB en total, o sea que hay margen de sobra.

El import de `faster_whisper` es perezoso (dentro de __init__, no a nivel de
modulo) para que el resto de la aplicacion pueda importar este archivo, o
TranscriptionProvider en general, sin necesitar torch/CUDA instalado -- eso
solo hace falta donde efectivamente se instancia FasterWhisperProvider
(chepita, la instancia GPU)."""
import inspect
from pathlib import Path

from src.modules.transcription.models.transcription_models import (
    TranscriptionResult,
    TranscriptionSegment,
    Word,
)
from src.modules.transcription.providers.transcription_provider import TranscriptionProvider
from src.modules.transcription.vocabulary import build_hotwords


class FasterWhisperProvider(TranscriptionProvider):
    def __init__(
        self,
        model_name: str = "large-v3-turbo",
        compute_type: str = "int8_float16",
        batch_size: int = 24,
        language: str = "es",
        vad_filter: bool = True,
        device: str = "cuda",
        hotwords: str | None = None,
    ):
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        self._batch_size = batch_size
        self._language = language
        self._vad_filter = vad_filter
        self._hotwords = hotwords if hotwords is not None else build_hotwords()

        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._pipeline = BatchedInferencePipeline(model=model)

        # `hotwords` se agrego a faster-whisper relativamente tarde y la firma
        # de BatchedInferencePipeline.transcribe no es identica entre versiones.
        # Se detecta una sola vez aca en vez de asumir: si la version instalada
        # no lo soporta, se transcribe igual (sin el sesgo de vocabulario) en
        # lugar de reventar toda la transcripcion por un TypeError de kwarg.
        self._supports_hotwords = "hotwords" in inspect.signature(
            self._pipeline.transcribe
        ).parameters

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        extra = (
            {"hotwords": self._hotwords}
            if self._supports_hotwords and self._hotwords
            else {}
        )
        segments_iter, info = self._pipeline.transcribe(
            str(audio_path),
            language=self._language,
            batch_size=self._batch_size,
            vad_filter=self._vad_filter,
            word_timestamps=True,
            **extra,
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
