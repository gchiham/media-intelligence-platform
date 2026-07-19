"""Capa de aplicacion (ver docs/PRD.md: API -> Application -> Domain <- Infrastructure).

MediaProcessingOrchestrator coordina el pipeline de segmentacion de noticias
invocando, en orden, los modulos ya validados de src/modules/ai/. No contiene
logica de IA ni de clipping propia -- cada paso sigue siendo independiente y
reutilizable por su cuenta; el orquestador solo decide el orden y pasa datos
de un paso al siguiente.

Trabaja con archivos locales (words.json + audio en disco). S3, PostgreSQL,
SQS y el resto del pipeline del PRD (clasificacion, entidades, matching,
editorial) se integran en una fase posterior -- ver "Preguntas abiertas" y
Roadmap en docs/PRD.md.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from src.modules.ai.clipping import ClipResult, clip_audio
from src.modules.ai.mapping import map_words_to_time
from src.modules.ai.providers.base import AIAnalysisProvider
from src.modules.ai.schemas import NewsSegment, Word


@dataclass
class ProcessAudioJob:
    words_json_path: Path
    audio_path: Path
    output_dir: Path
    padding_seconds: float = 2.0


@dataclass
class ProcessedNews:
    segment: NewsSegment
    start_time: float
    end_time: float
    clip: ClipResult


class MediaProcessingOrchestrator:
    def __init__(self, ai_provider: AIAnalysisProvider):
        self._ai_provider = ai_provider

    def process_audio(self, job: ProcessAudioJob) -> list[ProcessedNews]:
        """1. Localiza y carga el words.json del trabajo.
        2. AIAnalysisProvider.segment_news -> propone noticias por indice de palabra.
        3. map_words_to_time -> convierte cada propuesta a segundos reales + padding.
        4. clip_audio -> corta el audio original en el clip final.
        5. Devuelve la lista de noticias procesadas (sin persistir nada)."""
        words = self._load_words(job.words_json_path)

        segments = self._ai_provider.segment_news(words)

        processed: list[ProcessedNews] = []
        for i, segment in enumerate(segments):
            timing = map_words_to_time(segment, words, padding=job.padding_seconds)
            clip_path = job.output_dir / f"news_{i:03d}.mp3"
            clip = clip_audio(job.audio_path, clip_path, timing.start_time, timing.end_time)
            processed.append(
                ProcessedNews(
                    segment=segment,
                    start_time=timing.start_time,
                    end_time=timing.end_time,
                    clip=clip,
                )
            )

        return processed

    @staticmethod
    def _load_words(path: Path) -> list[Word]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Word.model_validate(item) for item in raw]
