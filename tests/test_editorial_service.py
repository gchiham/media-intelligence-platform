"""Pruebas de integracion del dominio Editorial contra PostgreSQL real
(docker-compose local) -- SELECT FOR UPDATE SKIP LOCKED y el resto de las
reglas de bloqueo/transaccion solo se pueden validar de verdad contra un
Postgres real, no con mocks. Se salta sola si Postgres no esta accesible.
Cada test limpia las filas que crea.
"""
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.infrastructure.db import registry  # noqa: F401 -- registra todos los modelos
from src.infrastructure.db.engine import get_engine
from src.modules.auth.models import RolUsuario, Usuario
from src.modules.editorial.exceptions import ColaVacia, NoticiaNoBloqueadaPorEditor
from src.modules.editorial.models import EstadoNoticia, Noticia, NoticiaVersion
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository
from src.modules.editorial.services import NoticiaService
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
            email=f"editor-test-{uuid.uuid4()}@adsignal.test",
            password_hash="x",
            nombre=f"Editor Test {i}",
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
        grabacion_id=grabacion_id,
        estado=EstadoNoticia.PENDIENTE,
        clip_inicio_seg=10.0,
        clip_fin_seg=20.0,
    )
    session.add(noticia)
    session.flush()

    version = NoticiaVersion(
        noticia_id=noticia.id,
        numero_version=1,
        titulo=titulo,
        resumen="resumen original",
        transcripcion_texto="transcripcion original",
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


@pytest.fixture
def service(session: Session):
    return NoticiaService(session, NoticiaRepository(session), NoticiaVersionRepository(session))


def test_start_review_toma_la_mas_antigua_de_la_cola(session, service, grabacion_id, editores):
    n1 = _crear_noticia_pendiente(session, grabacion_id, "primera en llegar")
    n2 = _crear_noticia_pendiente(session, grabacion_id, "segunda en llegar")
    try:
        tomada = service.start_review(editores[0])
        assert tomada.id == n1.id  # la mas vieja por created_at
        assert tomada.estado == EstadoNoticia.EN_REVISION
        assert tomada.asignado_a == editores[0]
        assert tomada.asignado_at is not None
    finally:
        _borrar_noticia(session, n1.id)
        _borrar_noticia(session, n2.id)


def test_start_review_cola_vacia_lanza_excepcion(session, service, editores):
    # No se fuerza la cola vacia mutando filas reales -- si ya hay noticias
    # PENDIENTE genuinas en la base (de otro trabajo), se salta el test en
    # vez de arriesgarse a corromper datos que no son de esta prueba.
    pendientes = session.scalar(
        select(func.count()).select_from(Noticia).where(Noticia.estado == EstadoNoticia.PENDIENTE)
    )
    if pendientes:
        pytest.skip(f"hay {pendientes} noticias PENDIENTE reales -- no se fuerza la cola vacia")

    with pytest.raises(ColaVacia):
        service.start_review(editores[0])


def test_start_review_concurrente_no_entrega_la_misma_noticia(session, grabacion_id, editores):
    n1 = _crear_noticia_pendiente(session, grabacion_id, "unica pendiente")
    try:
        engine = get_engine()
        with Session(engine) as s1, Session(engine) as s2:
            svc1 = NoticiaService(s1, NoticiaRepository(s1), NoticiaVersionRepository(s1))
            svc2 = NoticiaService(s2, NoticiaRepository(s2), NoticiaVersionRepository(s2))

            tomada1 = svc1.start_review(editores[0])
            assert tomada1.id == n1.id

            # s1 todavia no hizo commit visible mas alla de su propia sesion en
            # el momento del SELECT FOR UPDATE de s2 -- SKIP LOCKED hace que
            # s2 no vea n1 (esta bloqueada) y no encuentre nada mas.
            with pytest.raises(ColaVacia):
                svc2.start_review(editores[1])
    finally:
        _borrar_noticia(session, n1.id)


def test_save_draft_crea_nueva_version_y_preserva_historial(session, service, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "titulo original")
    try:
        service.start_review(editores[0])

        v2 = service.save_draft(noticia.id, editores[0], titulo="titulo corregido")
        assert v2.numero_version == 2
        assert v2.titulo == "titulo corregido"
        assert v2.resumen == "resumen original"  # heredado, no se toco
        assert v2.es_generada_por_ia is False
        assert v2.editado_por == editores[0]

        v3 = service.save_draft(noticia.id, editores[0], resumen="resumen corregido")
        assert v3.numero_version == 3
        assert v3.titulo == "titulo corregido"  # heredado de v2, no de v1
        assert v3.resumen == "resumen corregido"

        session.refresh(noticia)
        assert noticia.version_actual_id == v3.id

        todas = session.scalars(
            select(NoticiaVersion).where(NoticiaVersion.noticia_id == noticia.id).order_by(NoticiaVersion.numero_version)
        ).all()
        assert [v.numero_version for v in todas] == [1, 2, 3]  # historial completo, nada se borro
    finally:
        _borrar_noticia(session, noticia.id)


def test_save_draft_sin_bloqueo_lanza_excepcion(session, service, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "sin tomar")
    try:
        with pytest.raises(NoticiaNoBloqueadaPorEditor):
            service.save_draft(noticia.id, editores[0], titulo="no deberia funcionar")
    finally:
        _borrar_noticia(session, noticia.id)


def test_save_draft_editor_equivocado_lanza_excepcion(session, service, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "tomada por editor 0")
    try:
        service.start_review(editores[0])
        with pytest.raises(NoticiaNoBloqueadaPorEditor):
            service.save_draft(noticia.id, editores[1], titulo="editor equivocado")
    finally:
        _borrar_noticia(session, noticia.id)


def test_approve_cambia_estado_y_libera_bloqueo(session, service, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "para aprobar")
    try:
        service.start_review(editores[0])
        aprobada = service.approve(noticia.id, editores[0])
        assert aprobada.estado == EstadoNoticia.APROBADA
        assert aprobada.asignado_a is None
        assert aprobada.asignado_at is None
    finally:
        _borrar_noticia(session, noticia.id)


def test_reject_guarda_motivo_y_libera_bloqueo(session, service, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "para rechazar")
    try:
        service.start_review(editores[0])
        rechazada = service.reject(noticia.id, editores[0], motivo="no es una noticia real")
        assert rechazada.estado == EstadoNoticia.RECHAZADA
        assert rechazada.motivo_rechazo == "no es una noticia real"
        assert rechazada.asignado_a is None
    finally:
        _borrar_noticia(session, noticia.id)


def test_approve_sin_bloqueo_lanza_excepcion(session, service, grabacion_id, editores):
    noticia = _crear_noticia_pendiente(session, grabacion_id, "no tomada")
    try:
        with pytest.raises(NoticiaNoBloqueadaPorEditor):
            service.approve(noticia.id, editores[0])
    finally:
        _borrar_noticia(session, noticia.id)
