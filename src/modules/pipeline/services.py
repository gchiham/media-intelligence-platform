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


class PipelineRunService:
    def __init__(
        self,
        session: Session,
        pipeline_runs: PipelineRunRepository,
        noticias: NoticiaRepository,
        noticia_versiones: NoticiaVersionRepository,
        orchestrator: MediaProcessingOrchestrator,
    ):
        self._session = session
        self._pipeline_runs = pipeline_runs
        self._noticias = noticias
        self._noticia_versiones = noticia_versiones
        self._orchestrator = orchestrator

    def run(self, grabacion_id: uuid.UUID, job: ProcessAudioJob) -> PipelineRun:
        pipeline_run = PipelineRun(
            grabacion_id=grabacion_id,
            estado=EstadoPipelineRun.EN_PROGRESO,
            iniciado_at=datetime.now(timezone.utc),
        )
        self._pipeline_runs.add(pipeline_run)
        self._pipeline_runs.commit()

        try:
            processed_news = self._orchestrator.process_audio(job)

            for item in processed_news:
                noticia = Noticia(
                    grabacion_id=grabacion_id,
                    pipeline_run_id=pipeline_run.id,
                    estado=EstadoNoticia.PENDIENTE,
                    clip_inicio_seg=item.start_time,
                    clip_fin_seg=item.end_time,
                )
                self._noticias.add(noticia)
                self._session.flush()  # asigna noticia.id sin cerrar la transaccion

                # resumen/transcripcion_texto quedan vacios a proposito -- eso
                # es responsabilidad del modulo Editorial, fuera de alcance de
                # esta fase (solo se valida que la persistencia del pipeline
                # funcione de punta a punta).
                version = NoticiaVersion(
                    noticia_id=noticia.id,
                    numero_version=1,
                    titulo=item.segment.title,
                    resumen="",
                    transcripcion_texto="",
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
                "clips": [str(item.clip.output_path) for item in processed_news],
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
