"""Pruebas de integracion de la API de Pipeline via TestClient, contra
PostgreSQL real + OpenAI real + ffmpeg real -- sin mocks. Se saltan solas si
falta alguno de los tres. Cada test limpia lo que crea."""
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.main import app
from src.infrastructure.config import settings
from src.infrastructure.db import registry  # noqa: F401
from src.infrastructure.db.engine import get_engine
from src.modules.editorial.models import Noticia, NoticiaVersion
from src.modules.media.models import Medio, Programa
from src.modules.pipeline.models import PipelineRun
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
def client():
    return TestClient(app)


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
    assert grabacion is not None
    return grabacion.id


@pytest.fixture
def recording_files(grabacion_id, session: Session):
    """Coloca words.json + audio sintetico en la ruta local que
    LocalFileRecordingResolver espera para este recording_id (mismo convenio
    que src/modules/pipeline/resolvers.py)."""
    grabacion = session.get(Grabacion, grabacion_id)
    stem = grabacion.s3_key.rsplit(".", 1)[0].replace("/", "_")
    base_dir = settings.local_media_dir
    base_dir.mkdir(parents=True, exist_ok=True)

    words = json.loads((FIXTURES / "sample_words.json").read_text(encoding="utf-8"))
    words_path = base_dir / f"{stem}_words.json"
    words_path.write_text(json.dumps(words), encoding="utf-8")

    duration = words[-1]["end"] + 5
    audio_path = base_dir / f"{stem}.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration:.2f}",
         "-c:a", "libmp3lame", str(audio_path)],
        check=True, capture_output=True,
    )

    yield

    words_path.unlink(missing_ok=True)
    audio_path.unlink(missing_ok=True)


def _limpiar_pipeline_run(session: Session, pipeline_run_id: uuid.UUID):
    noticias = session.scalars(
        select(Noticia).where(Noticia.pipeline_run_id == pipeline_run_id)
    ).all()
    for n in noticias:
        n.version_actual_id = None
    session.flush()
    noticia_ids = [n.id for n in noticias]
    if noticia_ids:
        session.execute(NoticiaVersion.__table__.delete().where(NoticiaVersion.noticia_id.in_(noticia_ids)))
    session.execute(Noticia.__table__.delete().where(Noticia.pipeline_run_id == pipeline_run_id))
    session.execute(PipelineRun.__table__.delete().where(PipelineRun.id == pipeline_run_id))
    session.commit()


def test_process_recording_success(client, session, grabacion_id, recording_files):
    response = client.post("/api/v1/pipeline/process", json={"recording_id": str(grabacion_id)})
    assert response.status_code == 200
    body = response.json()
    try:
        assert body["status"] == "completado"
        assert body["news_generated"] == 2
        assert uuid.UUID(body["pipeline_run_id"])
    finally:
        _limpiar_pipeline_run(session, uuid.UUID(body["pipeline_run_id"]))
        shutil.rmtree(settings.local_media_dir / "clips" / str(grabacion_id), ignore_errors=True)


def test_process_recording_not_found_returns_404(client):
    response = client.post(
        "/api/v1/pipeline/process", json={"recording_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


def test_process_recording_missing_files_returns_409(client, grabacion_id):
    # grabacion_id valido, pero sin llamar al fixture recording_files -- los
    # archivos locales no existen.
    response = client.post("/api/v1/pipeline/process", json={"recording_id": str(grabacion_id)})
    assert response.status_code == 409


def test_process_recording_invalid_body_returns_422(client):
    response = client.post("/api/v1/pipeline/process", json={"recording_id": "not-a-uuid"})
    assert response.status_code == 422
