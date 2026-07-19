"""Pruebas de integracion de la API de Editorial via TestClient, contra
PostgreSQL real -- sin mocks. Cada test crea y limpia sus propias filas."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.main import app
from src.infrastructure.db import registry  # noqa: F401
from src.infrastructure.db.engine import get_engine
from src.modules.auth.models import RolUsuario, Usuario
from src.modules.editorial.models import EstadoNoticia, Noticia, NoticiaVersion
from src.modules.media.models import Medio, Programa
from src.modules.recordings.models import Grabacion


def _postgres_reachable() -> bool:
    try:
        with get_engine().connect():
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _postgres_reachable(), reason="requiere PostgreSQL accesible")


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
def editores(session: Session):
    creados = []
    for i in range(2):
        u = Usuario(
            email=f"api-test-editor-{uuid.uuid4()}@adsignal.test",
            password_hash="x",
            nombre=f"API Test Editor {i}",
            rol=RolUsuario.PERIODISTA,
        )
        session.add(u)
        creados.append(u)
    session.commit()
    yield [u.id for u in creados]
    for u in creados:
        session.delete(u)
    session.commit()


def _crear_noticia_pendiente(session: Session, grabacion_id, titulo: str) -> Noticia:
    noticia = Noticia(
        grabacion_id=grabacion_id, estado=EstadoNoticia.PENDIENTE,
        clip_inicio_seg=10.0, clip_fin_seg=20.0,
    )
    session.add(noticia)
    session.flush()
    version = NoticiaVersion(
        noticia_id=noticia.id, numero_version=1, titulo=titulo,
        resumen="resumen original", transcripcion_texto="transcripcion original",
        es_generada_por_ia=True,
    )
    session.add(version)
    session.flush()
    noticia.version_actual_id = version.id
    session.commit()
    return noticia


def _borrar_noticia(session: Session, noticia_id):
    noticia = session.get(Noticia, noticia_id)
    if noticia is None:
        return
    noticia.version_actual_id = None
    session.flush()
    session.execute(NoticiaVersion.__table__.delete().where(NoticiaVersion.noticia_id == noticia_id))
    session.execute(Noticia.__table__.delete().where(Noticia.id == noticia_id))
    session.commit()


def test_pending_lists_only_pendiente(client, session, grabacion_id):
    n1 = _crear_noticia_pendiente(session, grabacion_id, "noticia pendiente de prueba")
    try:
        response = client.get("/api/v1/news/pending")
        assert response.status_code == 200
        ids = [item["id"] for item in response.json()]
        assert str(n1.id) in ids
    finally:
        _borrar_noticia(session, n1.id)


def test_full_editorial_flow_success(client, session, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "titulo original")
    try:
        r1 = client.post("/api/v1/news/start-review", json={"editor_id": str(editores[0])})
        assert r1.status_code == 200
        assert r1.json()["id"] == str(noticia.id)
        assert r1.json()["status"] == "en_revision"
        assert r1.json()["assigned_to"] == str(editores[0])

        r2 = client.post(
            f"/api/v1/news/{noticia.id}/draft",
            json={"editor_id": str(editores[0]), "title": "titulo corregido"},
        )
        assert r2.status_code == 200
        assert r2.json()["version_number"] == 2
        assert r2.json()["title"] == "titulo corregido"
        assert r2.json()["is_ai_generated"] is False

        r3 = client.post(
            f"/api/v1/news/{noticia.id}/approve", json={"editor_id": str(editores[0])}
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "aprobada"
        assert r3.json()["assigned_to"] is None
        assert r3.json()["title"] == "titulo corregido"
    finally:
        _borrar_noticia(session, noticia.id)


def test_reject_flow(client, session, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "para rechazar")
    try:
        client.post("/api/v1/news/start-review", json={"editor_id": str(editores[0])})
        r = client.post(
            f"/api/v1/news/{noticia.id}/reject",
            json={"editor_id": str(editores[0]), "reason": "no es una noticia real"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rechazada"
    finally:
        _borrar_noticia(session, noticia.id)


def test_approve_not_found_returns_404(client, editores):
    response = client.post(
        f"/api/v1/news/{uuid.uuid4()}/approve", json={"editor_id": str(editores[0])}
    )
    assert response.status_code == 404


def test_draft_locked_by_another_editor_returns_409(client, session, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "tomada por editor 0")
    try:
        client.post("/api/v1/news/start-review", json={"editor_id": str(editores[0])})
        response = client.post(
            f"/api/v1/news/{noticia.id}/draft",
            json={"editor_id": str(editores[1]), "title": "no deberia funcionar"},
        )
        assert response.status_code == 409
    finally:
        _borrar_noticia(session, noticia.id)


def test_start_review_invalid_body_returns_422(client):
    response = client.post("/api/v1/news/start-review", json={"editor_id": "not-a-uuid"})
    assert response.status_code == 422


def test_start_review_cola_vacia_returns_204(client, session, editores):
    pendientes = session.scalar(
        select(func.count()).select_from(Noticia).where(Noticia.estado == EstadoNoticia.PENDIENTE)
    )
    if pendientes:
        pytest.skip(f"hay {pendientes} noticias PENDIENTE reales -- no se fuerza la cola vacia")

    response = client.post("/api/v1/news/start-review", json={"editor_id": str(editores[0])})
    assert response.status_code == 204
    assert response.content == b""
