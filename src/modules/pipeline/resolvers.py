"""Puerto que resuelve un recording_id (Grabacion) a los recursos fisicos que
necesita el pipeline: audio, words.json, y un directorio de salida para los
clips. Mismo patron que TranscriptionProvider/AIAnalysisProvider -- un solo
metodo, y ni PipelineRunService ni MediaProcessingOrchestrator lo conocen ni
saben si los archivos vienen de disco local o de S3. Solo la capa API lo usa,
antes de construir el ProcessAudioJob.

`LocalFileRecordingResolver` es la implementacion de desarrollo: busca los
archivos ya presentes en un directorio local (`settings.local_media_dir`).

`S3RecordingResolver` es la implementacion de produccion (docs/INGESTION_DESIGN.md):
descarga el audio de S3 a un directorio temporal, y escribe el words.json a
partir de `Transcripcion.segmentos` -- que ya vive en Postgres (Postgres es
la fuente de verdad del estado del sistema, nunca se vuelve a leer el
words.json de S3 aca; solo el audio, que no tiene copia en Postgres).
"""
import json
import shutil
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from src.modules.pipeline.exceptions import GrabacionNoEncontrada, RecursosNoDisponibles
from src.modules.recordings.repositories import GrabacionRepository, TranscripcionRepository


@dataclass
class RecordingResources:
    audio_path: Path
    words_json_path: Path
    output_dir: Path


class RecordingResolver(ABC):
    @abstractmethod
    def resolve(self, recording_id: uuid.UUID) -> RecordingResources:
        raise NotImplementedError

    def cleanup(self, resources: RecordingResources) -> None:
        """Borra copias temporales creadas por resolve() (ej. audio bajado de
        S3). No-op por default -- LocalFileRecordingResolver no posee esos
        archivos (son los originales de dev), asi que no hay nada que borrar."""


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


class ClipStorage(ABC):
    """Puerto para subir un clip ya recortado (local) a almacenamiento durable.
    Sin esto, MediaProcessingOrchestrator.clip_audio() escribe el clip en un
    directorio temporal del backend que nunca se sube a ningun lado -- se
    pierde si se limpia el disco o se recrea el contenedor."""

    @abstractmethod
    def upload(self, local_path: Path, key: str) -> str:
        """Sube local_path y devuelve la URI (s3://bucket/key) del resultado."""
        raise NotImplementedError


class S3ClipStorage(ClipStorage):
    def __init__(self, s3_client, bucket: str):
        self._s3 = s3_client
        self._bucket = bucket

    def upload(self, local_path: Path, key: str) -> str:
        self._s3.upload_file(str(local_path), self._bucket, key)
        return f"s3://{self._bucket}/{key}"


class NullClipStorage(ClipStorage):
    """Contraparte de LocalFileRecordingResolver para dev local sin AWS --
    no sube nada, deja el clip donde ya esta. clip_s3_uri queda NULL."""

    def upload(self, local_path: Path, key: str) -> str:
        raise RuntimeError("NullClipStorage no sube nada -- solo para dev local sin AWS")


class S3RecordingResolver(RecordingResolver):
    def __init__(
        self,
        grabaciones: GrabacionRepository,
        transcripciones: TranscripcionRepository,
        s3_client,
        capture_bucket: str,
        work_dir: Path,
    ):
        self._grabaciones = grabaciones
        self._transcripciones = transcripciones
        self._s3 = s3_client
        self._capture_bucket = capture_bucket
        self._work_dir = work_dir

    def resolve(self, recording_id: uuid.UUID) -> RecordingResources:
        grabacion = self._grabaciones.get_by_id(recording_id)
        if grabacion is None:
            raise GrabacionNoEncontrada(recording_id)

        transcripcion = self._transcripciones.get_by_grabacion_id(recording_id)
        if transcripcion is None:
            raise RecursosNoDisponibles(
                recording_id, "todavia no hay Transcripcion (chepita no ha terminado, o el "
                "TranscriptionResultConsumer no proceso el resultado)"
            )

        stem = grabacion.s3_key.rsplit(".", 1)[0].replace("/", "_")
        local_dir = self._work_dir / str(recording_id)
        local_dir.mkdir(parents=True, exist_ok=True)

        audio_path = local_dir / f"{stem}.mp3"
        self._s3.download_file(self._capture_bucket, grabacion.s3_key, str(audio_path))

        words_json_path = local_dir / f"{stem}_words.json"
        words_json_path.write_text(
            json.dumps(transcripcion.segmentos["words"], ensure_ascii=False), encoding="utf-8"
        )

        output_dir = local_dir / "clips"
        return RecordingResources(
            audio_path=audio_path, words_json_path=words_json_path, output_dir=output_dir,
        )

    def cleanup(self, resources: RecordingResources) -> None:
        # audio_path.parent es local_dir (ver resolve()) -- el audio bajado
        # de S3 y el words.json escrito ahi no tienen ningun uso despues de
        # correr el pipeline (Postgres ya tiene la Transcripcion; el clip, si
        # se subio, ya se borro en PipelineRunService). Sin esto el disco del
        # backend se llena solo -- 208 grabaciones x ~35MB de audio agotaron
        # los 6.8GB de /tmp en Clipper a mitad de un batch.
        shutil.rmtree(resources.audio_path.parent, ignore_errors=True)
