"""Dependencias de FastAPI: sesion de base de datos por request, y
constructores de los servicios/repositorios/resolvers ya existentes. No
contiene logica de negocio -- solo resuelve e inyecta lo que cada servicio
necesita, para que los routers nunca instancien nada a mano.

`get_db_session` abre una Session nueva por request y la cierra siempre
(try/finally) al terminar, incluso si el request falla. FastAPI cachea el
resultado de cada dependencia dentro de un mismo request, asi que aunque
`get_db_session` aparezca como sub-dependencia de varios `Depends()` en el
mismo endpoint, se abre una sola sesion por request, no una por dependencia.
"""
import tempfile
from collections.abc import Generator
from pathlib import Path

import boto3
from fastapi import Depends
from sqlalchemy.orm import Session

from src.application.orchestrator import MediaProcessingOrchestrator
from src.infrastructure.config import settings
from src.infrastructure.db.engine import get_engine
from src.modules.ai.providers.anthropic_provider import AnthropicAnalysisProvider
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository
from src.modules.editorial.services import NoticiaService
from src.modules.pipeline.repositories import PipelineRunRepository
from src.modules.pipeline.resolvers import (
    LocalFileRecordingResolver,
    RecordingResolver,
    S3RecordingResolver,
)
from src.modules.pipeline.services import PipelineRunService
from src.modules.recordings.repositories import GrabacionRepository, TranscripcionRepository


def get_db_session() -> Generator[Session, None, None]:
    session = Session(get_engine())
    try:
        yield session
    finally:
        session.close()


def get_noticia_service(session: Session = Depends(get_db_session)) -> NoticiaService:
    return NoticiaService(
        session=session,
        noticias=NoticiaRepository(session),
        versiones=NoticiaVersionRepository(session),
    )


def get_noticia_repository(session: Session = Depends(get_db_session)) -> NoticiaRepository:
    """Solo para lecturas que no son una operacion de negocio (ej. listar la
    cola) -- las escrituras siempre pasan por NoticiaService, nunca por aqui."""
    return NoticiaRepository(session)


def get_noticia_version_repository(session: Session = Depends(get_db_session)) -> NoticiaVersionRepository:
    """Solo para armar respuestas HTTP (leer el titulo/resumen de la version
    actual) -- NoticiaService no expone esto porque no es una operacion de
    negocio, es una necesidad de serializacion de la capa de transporte."""
    return NoticiaVersionRepository(session)


def get_recording_resolver(session: Session = Depends(get_db_session)) -> RecordingResolver:
    """`settings.recording_resolver` decide la implementacion -- "local" en
    dev (sin credenciales AWS), "s3" en produccion. Ver docs/INGESTION_DESIGN.md."""
    if settings.recording_resolver == "s3":
        return S3RecordingResolver(
            grabaciones=GrabacionRepository(session),
            transcripciones=TranscripcionRepository(session),
            s3_client=boto3.client("s3", region_name=settings.aws_region),
            capture_bucket=settings.capture_bucket,
            work_dir=Path(tempfile.gettempdir()) / "media-intel-pipeline",
        )
    return LocalFileRecordingResolver(
        grabaciones=GrabacionRepository(session), base_dir=settings.local_media_dir,
    )


def get_pipeline_run_service(session: Session = Depends(get_db_session)) -> PipelineRunService:
    # Sin fallback a otro modelo a proposito -- se prefiere que el
    # PipelineRun quede en ERROR (AnthropicAnalysisProvider ya reintenta 3
    # veces con backoff antes de rendirse) a degradar la calidad de
    # extraccion cayendo a un modelo mas chico.
    orchestrator = MediaProcessingOrchestrator(
        ai_provider=AnthropicAnalysisProvider(
            api_key=settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else "",
            model=settings.anthropic_model,
        )
    )
    return PipelineRunService(
        session=session,
        pipeline_runs=PipelineRunRepository(session),
        noticias=NoticiaRepository(session),
        noticia_versiones=NoticiaVersionRepository(session),
        orchestrator=orchestrator,
    )
