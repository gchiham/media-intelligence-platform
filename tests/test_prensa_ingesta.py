"""Ingesta de prensa contra PostgreSQL real (docker-compose local). El dedup se
apoya en ON CONFLICT sobre uq_articulos_fuente_guid, que solo existe de verdad
en Postgres. Se salta solo si no esta accesible. Cada test limpia lo que crea.

La red se sustituye por un descargador falso: lo que importa probar es que el
cron sea idempotente y respete el corte, no que urllib sepa hacer un GET.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.infrastructure.db import registry  # noqa: F401 -- registra todos los modelos
from src.infrastructure.db.engine import get_engine
from src.modules.media.models import Medio, TipoMedio
from src.modules.prensa.feeds import Descarga, ErrorDescarga
from src.modules.prensa.models import Articulo, FuenteWeb, TipoFuente
from src.modules.prensa.services import IngestaPrensaService


def _postgres_reachable() -> bool:
    try:
        with get_engine().connect():
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _postgres_reachable(), reason="requiere PostgreSQL accesible")

AHORA = datetime.now(timezone.utc)


def _rss(*fechas_guids, imagen: str | None = None) -> bytes:
    enclosure = f'<enclosure url="{imagen}" type="image/jpeg"/>' if imagen else ""
    items = "".join(
        f"<item><title>Nota {g}</title><link>https://x.test/{g}</link>"
        f"<guid>{g}</guid><pubDate>{f.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>{enclosure}</item>"
        for f, g in fechas_guids
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'.encode()


@pytest.fixture
def session():
    with Session(get_engine()) as s:
        yield s


@pytest.fixture
def fuente(session: Session):
    medio = Medio(
        codigo=f"test_digital_{uuid.uuid4().hex[:8]}",
        nombre="Medio de prueba",
        tipo=TipoMedio.DIGITAL,
    )
    session.add(medio)
    session.flush()
    f = FuenteWeb(medio_id=medio.id, url=f"https://x.test/{uuid.uuid4()}/feed/", tipo=TipoFuente.RSS)
    session.add(f)
    session.commit()
    yield f
    session.execute(delete(Articulo).where(Articulo.fuente_id == f.id))
    session.execute(delete(FuenteWeb).where(FuenteWeb.id == f.id))
    session.execute(delete(Medio).where(Medio.id == medio.id))
    session.commit()


def _contar(session: Session, fuente: FuenteWeb) -> int:
    return session.scalar(select(func.count()).select_from(Articulo).where(Articulo.fuente_id == fuente.id))


def test_segunda_pasada_no_duplica(session: Session, fuente: FuenteWeb):
    """El caso que define al cron: ve los mismos items ~96 veces por dia."""
    cuerpo = _rss((AHORA, "a"), (AHORA, "b"))
    service = IngestaPrensaService(session, lambda *a, **k: Descarga(True, cuerpo, None, None))

    primera = service.procesar(fuente)
    segunda = service.procesar(fuente)

    assert (primera.nuevos, primera.repetidos) == (2, 0)
    assert (segunda.nuevos, segunda.repetidos) == (0, 2)
    assert _contar(session, fuente) == 2


def test_guid_repetido_dentro_del_mismo_feed_se_colapsa(session: Session, fuente: FuenteWeb):
    cuerpo = _rss((AHORA, "a"), (AHORA, "a"))
    service = IngestaPrensaService(session, lambda *a, **k: Descarga(True, cuerpo, None, None))
    assert service.procesar(fuente).nuevos == 1


def test_fecha_corte_deja_afuera_el_historial(session: Session, fuente: FuenteWeb):
    """Televicentro arrastra 625 h de historial en 9 items: sin corte, el primer
    poll mete notas de hace tres semanas como si fueran de hoy."""
    fuente.fecha_corte = AHORA - timedelta(hours=1)
    session.commit()
    cuerpo = _rss((AHORA, "nueva"), (AHORA - timedelta(days=26), "vieja"))
    service = IngestaPrensaService(session, lambda *a, **k: Descarga(True, cuerpo, None, None))

    r = service.procesar(fuente)

    assert (r.nuevos, r.previos_al_corte) == (1, 1)
    assert session.scalar(select(Articulo.guid).where(Articulo.fuente_id == fuente.id)) == "nueva"


def test_304_no_toca_articulos_y_conserva_validadores(session: Session, fuente: FuenteWeb):
    fuente.etag, fuente.last_modified = 'W/"abc"', "Wed, 19 Aug 2026 04:00:00 GMT"
    session.commit()
    service = IngestaPrensaService(
        session, lambda url, etag, lm, **k: Descarga(False, None, etag, lm)
    )

    r = service.procesar(fuente)

    assert r.estado == "sin_cambios"
    assert _contar(session, fuente) == 0
    assert fuente.etag == 'W/"abc"'
    assert fuente.ultimo_poll_at is not None


def test_error_no_rompe_la_pasada_y_queda_contado(session: Session, fuente: FuenteWeb):
    """Una fuente caida no puede tumbar a las otras 19 ni pasar desapercibida."""

    def cae(*a, **k):
        raise ErrorDescarga("HTTP 403")

    r = IngestaPrensaService(session, cae).procesar(fuente)

    assert r.estado == "error"
    session.refresh(fuente)
    assert fuente.errores_consecutivos == 1
    assert fuente.ultimo_resultado == "error: HTTP 403"


def test_imagen_url_se_persiste(session: Session, fuente: FuenteWeb):
    cuerpo = _rss((AHORA, "a"), imagen="https://x.test/foto.jpg")
    service = IngestaPrensaService(session, lambda *a, **k: Descarga(True, cuerpo, None, None))

    service.procesar(fuente)

    assert session.scalar(
        select(Articulo.imagen_url).where(Articulo.fuente_id == fuente.id)
    ) == "https://x.test/foto.jpg"


def test_etag_solo_se_guarda_despues_de_persistir(session: Session, fuente: FuenteWeb):
    """Si se guardara antes, un fallo al insertar dejaria la fuente pidiendo 304
    sobre articulos que nunca entraron: se perderian para siempre."""
    cuerpo = _rss((AHORA, "a"))
    service = IngestaPrensaService(
        session, lambda *a, **k: Descarga(True, cuerpo, 'W/"nuevo"', None)
    )

    service.procesar(fuente)

    session.refresh(fuente)
    assert fuente.etag == 'W/"nuevo"'
    assert _contar(session, fuente) == 1
