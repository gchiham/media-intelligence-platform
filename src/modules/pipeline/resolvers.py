"""Puerto que resuelve un recording_id (Grabacion) a los recursos fisicos que
necesita el pipeline: audio, words.json, y un directorio de salida para los
clips. Mismo patron que TranscriptionProvider/AIAnalysisProvider -- un solo
metodo, y ni PipelineRunService ni MediaProcessingOrchestrator lo conocen ni
saben si los archivos vienen de disco local o de S3. Solo la capa API lo usa,
antes de construir el ProcessAudioJob.

`LocalFileRecordingResolver` es la implementacion de esta fase: busca los
archivos ya presentes en un directorio local (`settings.local_media_dir`).
Cuando exista integracion real con S3, la migracion es escribir un
`S3RecordingResolver` que descargue a un directorio temporal y devuelva las
mismas rutas locales -- el resto de la aplicacion no cambia.
"""
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from src.modules.pipeline.exceptions import GrabacionNoEncontrada, RecursosNoDisponibles
from src.modules.recordings.repositories import GrabacionRepository


@dataclass
class RecordingResources:
    audio_path: Path
    words_json_path: Path
    output_dir: Path


class RecordingResolver(ABC):
    @abstractmethod
    def resolve(self, recording_id: uuid.UUID) -> RecordingResources:
        raise NotImplementedError


class LocalFileRecordingResolver(RecordingResolver):
    def __init__(self, grabaciones: GrabacionRepository, base_dir: Path):
        self._grabaciones = grabaciones
        self._base_dir = base_dir

    def resolve(self, recording_id: uuid.UUID) -> RecordingResources:
        grabacion = self._grabaciones.get_by_id(recording_id)
        if grabacion is None:
            raise GrabacionNoEncontrada(recording_id)

        # s3_key trae slashes (ej. "radio_satelite/2026/06/2026-06-26T23Z.mp3")
        # -- se aplana a un nombre de archivo local valido.
        stem = grabacion.s3_key.rsplit(".", 1)[0].replace("/", "_")
        audio_path = self._base_dir / f"{stem}.mp3"
        words_json_path = self._base_dir / f"{stem}_words.json"
        output_dir = self._base_dir / "clips" / str(recording_id)

        faltantes = [
            str(p) for p in (audio_path, words_json_path) if not p.exists()
        ]
        if faltantes:
            raise RecursosNoDisponibles(recording_id, f"archivos no encontrados: {', '.join(faltantes)}")

        return RecordingResources(
            audio_path=audio_path, words_json_path=words_json_path, output_dir=output_dir,
        )
