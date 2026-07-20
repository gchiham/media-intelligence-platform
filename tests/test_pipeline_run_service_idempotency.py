"""PipelineRunService.run() no debe reprocesar una Grabacion que ya tiene un
PipelineRun COMPLETADO (docs/INGESTION_DESIGN.md, punto 6 -- evitar Noticia
duplicada si el endpoint se llama dos veces para la misma grabacion).

Postgres real (igual que test_pipeline_run_service_e2e.py); el orchestrator
se mockea porque esta prueba valida la idempotencia de PipelineRunService,
no el pipeline de IA/clipping en si (ya cubierto por test_orchestrator_e2e.py).
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.application.orchestrator import MediaProcessingOrchestrator, ProcessAudioJob
from src.infrastructure.db import registry  # noqa: F401
from src.infrastructure.db.engine import get_engine
from src.modules.editorial.models import Noticia, NoticiaVersion
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository
from src.modules.media.models import Medio, Programa, TipoMedio
from src.modules.pipeline.models import PipelineRun
from src.modules.pipeline.repositories import PipelineRunRepository
from src.modules.pipeline.services import PipelineRunService
from src.modules.recordings.models import EstadoGrabacion, Grabacion


def _postgres_reachable() -> bool:
    try:
        with get_engine().connect():
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _postgres_reachable(), reason="requiere PostgreSQL accesible")


@pytest.fixture
def session():
    with Session(get_engine()) as s:
        yield s


@pytest.fixture
def grabacion(session: Session):
    medio = Medio(codigo=f"test_idem_{uuid.uuid4().hex[:8]}", nombre="Test", tipo=TipoMedio.RADIO)
    session.add(medio)
    session.flush()
    programa = Programa(medio_id=medio.id, nombre="Transmision continua")
    session.add(programa)
    session.flush()
    grab = Grabacion(
        programa_id=programa.id,
        s3_key=f"{medio.codigo}/2026/07/2026-07-20T15Z.mp3",
        fecha_inicio=datetime(2026, 7, 20, 15, tzinfo=timezone.utc),
        fecha_fin=datetime(2026, 7, 20, 16, tzinfo=timezone.utc),
        estado=EstadoGrabacion.PROCESADA,
    )
    session.add(grab)
    session.commit()
    yield grab

    session.execute(delete(NoticiaVersion).where(NoticiaVersion.noticia_id.in_(
        session.query(Noticia.id).filter(Noticia.grabacion_id == grab.id)
    )))
    session.execute(delete(Noticia).where(Noticia.grabacion_id == grab.id))
    session.execute(delete(PipelineRun).where(PipelineRun.grabacion_id == grab.id))
    session.execute(delete(Grabacion).where(Grabacion.id == grab.id))
    session.execute(delete(Programa).where(Programa.id == programa.id))
    session.execute(delete(Medio).where(Medio.id == medio.id))
    session.commit()


def test_second_run_returns_existing_pipeline_run_without_reprocessing(session, grabacion, tmp_path: Path):
    orchestrator = MagicMock(spec=MediaProcessingOrchestrator)
    orchestrator.process_audio.return_value = []

    service = PipelineRunService(
        session=session,
        pipeline_runs=PipelineRunRepository(session),
        noticias=NoticiaRepository(session),
        noticia_versiones=NoticiaVersionRepository(session),
        orchestrator=orchestrator,
    )
    job = ProcessAudioJob(
        words_json_path=tmp_path / "words.json", audio_path=tmp_path / "audio.mp3", output_dir=tmp_path / "clips",
    )

    first_run = service.run(grabacion.id, job)
    second_run = service.run(grabacion.id, job)

    assert first_run.id == second_run.id
    orchestrator.process_audio.assert_called_once()
