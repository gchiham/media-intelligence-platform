"""Logica de negocio de Pipeline Runs: ejecuta MediaProcessingOrchestrator
(sin modificarlo -- Editorial y el resto de modulos siguen sin logica de
negocio propia en esta fase) y persiste el resultado -- PipelineRun, Noticia,
NoticiaVersion -- en PostgreSQL. Ver docs/BACKEND_ARCHITECTURE.md.

Transacciones: el PipelineRun se marca EN_PROGRESO y se commitea de inmediato
(NFR-012: toda tarea asincrona debe quedar registrada y trazable, incluso si
el proceso se cae despues). El resto (Noticia + NoticiaVersion por cada
resultado, y el estado final del run) se hace en una sola transaccion --
o se persiste todo o no se persiste nada de eso, sin dejar noticias a medias.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.application.orchestrator import MediaProcessingOrchestrator, ProcessAudioJob
from src.modules.editorial.models import EstadoNoticia, Noticia, NoticiaVersion
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository
from src.modules.pipeline.models import EstadoPipelineRun, PipelineRun
from src.modules.pipeline.repositories import PipelineRunRepository
from src.modules.pipeline.resolvers import ClipStorage
from src.shared.logging_utils import get_logger

logger = get_logger("pipeline_run_service")


class PipelineRunService:
    def __init__(
        self,
        session: Session,
        pipeline_runs: PipelineRunRepository,
        noticias: NoticiaRepository,
        noticia_versiones: NoticiaVersionRepository,
        orchestrator: MediaProcessingOrchestrator,
        clip_storage: ClipStorage,
    ):
        self._session = session
        self._pipeline_runs = pipeline_runs
        self._noticias = noticias
        self._noticia_versiones = noticia_versiones
        self._orchestrator = orchestrator
        self._clip_storage = clip_storage

    def run(self, grabacion_id: uuid.UUID, job: ProcessAudioJob) -> PipelineRun:
        # Idempotencia (docs/INGESTION_DESIGN.md, punto 6): si esta Grabacion
        # ya tiene un PipelineRun COMPLETADO, devolverlo tal cual en vez de
        # correr el pipeline de nuevo -- evita Noticia/NoticiaVersion
        # duplicadas si el endpoint se llama dos veces para la misma
        # grabacion (reintento del cliente, doble click, etc.).
        existing = self._pipeline_runs.get_completado_by_grabacion_id(grabacion_id)
        if existing is not None:
            return existing

        pipeline_run = PipelineRun(
            grabacion_id=grabacion_id,
            estado=EstadoPipelineRun.EN_PROGRESO,
            iniciado_at=datetime.now(timezone.utc),
        )
        self._pipeline_runs.add(pipeline_run)
        self._pipeline_runs.commit()

        try:
            processed_news = self._orchestrator.process_audio(job)

            for i, item in enumerate(processed_news):
                clip_s3_uri = self._upload_clip(grabacion_id, i, item.clip.output_path)

                noticia = Noticia(
                    grabacion_id=grabacion_id,
                    pipeline_run_id=pipeline_run.id,
                    estado=EstadoNoticia.PENDIENTE,
                    clip_inicio_seg=item.start_time,
                    clip_fin_seg=item.end_time,
                    clip_s3_uri=clip_s3_uri,
                )
                self._noticias.add(noticia)
                self._session.flush()  # asigna noticia.id sin cerrar la transaccion

                # tema_id/subtema_id y la resolucion de personas/organizaciones/
                # lugares contra el catalogo de Entidad quedan fuera de esta
                # fase a proposito -- news_type/keywords/entidades del LLM se
                # guardan crudos en metadatos_ia, sin resolver todavia.
                version = NoticiaVersion(
                    noticia_id=noticia.id,
                    numero_version=1,
                    titulo=item.segment.title,
                    resumen=item.segment.summary,
                    transcripcion_texto=item.text,
                    confianza={"overall": item.segment.confidence},
                    metadatos_ia={
                        "news_type": item.segment.news_type.value,
                        "keywords": item.segment.keywords,
                        "people": item.segment.people,
                        "organizations": item.segment.organizations,
                        "locations": item.segment.locations,
                    },
                    es_generada_por_ia=True,
                )
                self._noticia_versiones.add(version)
                self._session.flush()

                noticia.version_actual_id = version.id

            pipeline_run.estado = EstadoPipelineRun.COMPLETADO
            pipeline_run.finalizado_at = datetime.now(timezone.utc)
            pipeline_run.noticias_generadas = len(processed_news)
            pipeline_run.metadatos = {
                "padding_seconds": job.padding_seconds,
                "segmentos_detectados": [
                    {"titulo": item.segment.title, "confidence": item.segment.confidence}
                    for item in processed_news
                ],
            }
            self._session.commit()

        except Exception as exc:
            self._session.rollback()
            pipeline_run.estado = EstadoPipelineRun.ERROR
            pipeline_run.error_mensaje = str(exc)
            pipeline_run.finalizado_at = datetime.now(timezone.utc)
            self._session.commit()
            raise

        return pipeline_run

    def _upload_clip(self, grabacion_id: uuid.UUID, index: int, local_path) -> str | None:
        # Un fallo de subida no debe tumbar el PipelineRun completo -- la
        # noticia y su texto ya son validos sin el audio; clip_s3_uri
        # simplemente queda NULL y se puede reintentar la subida despues.
        key = f"{grabacion_id}/news_{index:03d}.mp3"
        try:
            uri = self._clip_storage.upload(local_path, key)
        except Exception as exc:
            logger.warning(
                "fallo al subir clip a S3, continua sin el",
                extra={"extra_fields": {"grabacion_id": str(grabacion_id), "key": key, "error": str(exc)}},
            )
            return None

        local_path.unlink(missing_ok=True)
        return uri
