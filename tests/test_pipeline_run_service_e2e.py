"""Prueba end-to-end real: PostgreSQL real (docker-compose local) + OpenAI real
+ ffmpeg real. Confirma que al terminar process_audio(), PipelineRun, Noticia
y NoticiaVersion quedan persistidos correctamente y correctamente enlazados.

Reusa el mismo fixture de transcript (tests/fixtures/sample_words.json) y el
mismo mecanismo de audio sintetico (tono via ffmpeg) que
test_orchestrator_e2e.py -- el motor de segmentacion/mapeo/clipping ya esta
validado ahi; esta prueba valida especificamente la capa nueva: persistencia
en Postgres. Se salta sola si falta OPENAI_API_KEY, ffmpeg, o si Postgres no
esta accesible. Limpia (borra) las filas que crea al terminar.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.application.orchestrator import MediaProcessingOrchestrator, ProcessAudioJob
from src.infrastructure.config import settings
from src.infrastructure.db import registry  # noqa: F401 -- registra todos los modelos (FKs cruzadas entre modulos)
from src.infrastructure.db.engine import get_engine
from src.modules.ai.providers.openai_provider import OpenAIAnalysisProvider
from src.modules.editorial.models import Noticia, NoticiaVersion
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository
from src.modules.media.models import Medio, Programa
from src.modules.pipeline.models import EstadoPipelineRun, PipelineRun
from src.modules.pipeline.repositories import PipelineRunRepository
from src.modules.pipeline.services import PipelineRunService
from src.modules.recordings.models import Grabacion

FIXTURES = Path(__file__).parent / "fixtures"


def _postgres_reachable() -> bool:
    try:
        with get_engine().connect():
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(settings.openai_api_key is None, reason="requiere OPENAI_API_KEY"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requiere ffmpeg instalado"),
    pytest.mark.skipif(not _postgres_reachable(), reason="requiere PostgreSQL accesible"),
]


@pytest.fixture
def synthetic_audio(tmp_path: Path) -> Path:
    words = json.loads((FIXTURES / "sample_words.json").read_text(encoding="utf-8"))
    duration = words[-1]["end"] + 5
    audio_path = tmp_path / "source.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration:.2f}",
            "-c:a", "libmp3lame", str(audio_path),
        ],
        check=True, capture_output=True,
    )
    return audio_path


@pytest.fixture
def session():
    with Session(get_engine()) as s:
        yield s


@pytest.fixture
def grabacion_id(session: Session):
    medio = session.scalar(select(Medio).where(Medio.codigo == "radio_satelite"))
    assert medio is not None, "correr scripts/seed_medios.py antes de este test"
    programa = session.scalar(select(Programa).where(Programa.medio_id == medio.id))
    grabacion = session.scalar(select(Grabacion).where(Grabacion.programa_id == programa.id))
    assert grabacion is not None, "falta la Grabacion de prueba (radio_satelite)"
    return grabacion.id


def test_process_audio_persists_pipeline_run_and_news(
    tmp_path: Path, synthetic_audio: Path, session: Session, grabacion_id,
):
    orchestrator = MediaProcessingOrchestrator(
        ai_provider=OpenAIAnalysisProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
        )
    )
    service = PipelineRunService(
        session=session,
        pipeline_runs=PipelineRunRepository(session),
        noticias=NoticiaRepository(session),
        noticia_versiones=NoticiaVersionRepository(session),
        orchestrator=orchestrator,
    )
    job = ProcessAudioJob(
        words_json_path=FIXTURES / "sample_words.json",
        audio_path=synthetic_audio,
        output_dir=tmp_path / "clips",
    )

    pipeline_run = service.run(grabacion_id, job)
    try:
        assert pipeline_run.estado == EstadoPipelineRun.COMPLETADO
        assert pipeline_run.noticias_generadas == 2
        assert pipeline_run.iniciado_at is not None
        assert pipeline_run.finalizado_at is not None
        assert pipeline_run.error_mensaje is None
        assert pipeline_run.metadatos["padding_seconds"] == 2.0
        assert len(pipeline_run.metadatos["clips"]) == 2

        noticias = session.scalars(
            select(Noticia).where(Noticia.pipeline_run_id == pipeline_run.id)
        ).all()
        assert len(noticias) == 2

        for noticia in noticias:
            assert noticia.grabacion_id == grabacion_id
            assert noticia.clip_fin_seg > noticia.clip_inicio_seg
            assert noticia.version_actual_id is not None

            version = session.get(NoticiaVersion, noticia.version_actual_id)
            assert version is not None
            assert version.noticia_id == noticia.id
            assert version.numero_version == 1
            assert version.es_generada_por_ia is True
            assert version.titulo  # no vacio
    finally:
        # limpieza -- no dejar basura de prueba en la base real. Orden: primero
        # desvincular version_actual_id (FK hacia noticia_versiones), despues
        # borrar versiones, noticias, y por ultimo el pipeline_run.
        noticias = session.scalars(
            select(Noticia).where(Noticia.pipeline_run_id == pipeline_run.id)
        ).all()
        noticia_ids = [n.id for n in noticias]
        for noticia in noticias:
            noticia.version_actual_id = None
        session.flush()
        if noticia_ids:
            session.execute(
                NoticiaVersion.__table__.delete().where(NoticiaVersion.noticia_id.in_(noticia_ids))
            )
        session.execute(Noticia.__table__.delete().where(Noticia.pipeline_run_id == pipeline_run.id))
        session.execute(PipelineRun.__table__.delete().where(PipelineRun.id == pipeline_run.id))
        session.commit()
