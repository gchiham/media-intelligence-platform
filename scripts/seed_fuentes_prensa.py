"""Da de alta los medios digitales y sus fuentes web (RSS / sitemap de noticias).

Todas las URLs de aca fueron probadas contra el endpoint real el 2026-08-19:
devuelven XML valido con items frescos. Idempotente: se puede correr varias
veces sin duplicar.

Uso:
    python scripts/seed_fuentes_prensa.py                 # corte = ahora
    python scripts/seed_fuentes_prensa.py --corte-horas 24
    python scripts/seed_fuentes_prensa.py --sin-corte     # traga todo el historial

Sobre el corte: sin el, la primera pasada ingiere todo lo que traiga el feed.
Televicentro arrastra 625 h en 9 items y ContraCorriente 546 h en 15 -- se
llenaria la base de notas de hace tres semanas como si fueran de hoy. Es el
mismo cuidado que hubo que tener al dar de alta canal_10 (commit 4c80416).
El corte solo aplica a las fuentes que se crean en esta corrida.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.media.models import Medio, TipoMedio  # noqa: E402
from src.modules.prensa.models import FuenteWeb, TipoFuente  # noqa: E402

# Medios nativos de web: no los captura mediaCAP, solo existen como sitio.
MEDIOS_DIGITALES = [
    ("proceso_digital", "Proceso Digital"),
    ("la_prensa", "La Prensa"),
    ("el_heraldo", "El Heraldo"),
    ("diez", "Diez"),
    ("el_pais_hn", "El Pais Honduras"),
    ("hondudiario", "Hondudiario"),
    ("confidencial_hn", "Confidencial HN"),
    ("el_libertador", "El Libertador"),
    ("criterio_hn", "Criterio.hn"),
    ("contracorriente", "ContraCorriente"),
    ("el_pulso", "El Pulso"),
    ("reporteros_investigacion", "Reporteros de Investigacion"),
    ("expediente_publico", "Expediente Publico"),
    ("tunota", "TuNota"),
    ("el_mundo", "El Mundo"),
    ("radio_progreso", "Radio Progreso"),
    ("televicentro", "Televicentro"),
]

# (codigo_medio, url, tipo). Los primeros tres cuelgan de medios que YA existen
# en seed_medios.py: son la misma empresa periodistica publicando en dos
# canales, y conviene que las apariciones queden bajo el mismo Medio para que
# un reporte por cliente no las cuente como dos medios distintos.
#
# REVISAR: la equivalencia oncenoticias.hn -> canal_11 (Once Noticias es el
# noticiero de Canal 11) es un criterio mio, no un dato del sistema de captura.
# Si en el negocio se manejan como medios separados, sacalo de aca y agregalo
# a MEDIOS_DIGITALES con codigo propio.
FUENTES = [
    ("hch_tv", "https://hch.tv/feed/", "rss"),
    ("canal_11", "https://www.oncenoticias.hn/feed/", "rss"),
    ("radio_america", "https://radioamerica.hn/feed/", "rss"),
    # --- medios digitales
    ("proceso_digital", "https://proceso.hn/feed/", "rss"),
    ("el_pais_hn", "https://www.elpais.hn/feed/", "rss"),
    ("hondudiario", "https://www.hondudiario.com/feed/", "rss"),
    ("confidencial_hn", "https://confidencialhn.com/feed/", "rss"),
    ("el_libertador", "https://ellibertador.hn/feed/", "rss"),
    ("criterio_hn", "https://criterio.hn/feed/", "rss"),
    ("contracorriente", "https://contracorriente.red/feed/", "rss"),
    ("el_pulso", "https://www.elpulso.hn/feed/", "rss"),
    ("reporteros_investigacion", "https://reporterosdeinvestigacion.com/feed/", "rss"),
    ("expediente_publico", "https://expedientepublico.org/feed/", "rss"),
    ("tunota", "https://tunota.com/feed/", "rss"),
    ("el_mundo", "https://www.elmundo.hn/feed/", "rss"),
    ("radio_progreso", "https://radioprogresohn.net/feed/", "rss"),
    ("televicentro", "https://www.televicentro.hn/feed/", "rss"),
    # Grupo OPSA corre Liferay y no expone RSS por ninguna ruta (/rss, /feed,
    # /arc/... todas 404). El sitemap de Google News es un reemplazo mejor que
    # un RSS tipico: mas historial y fecha de publicacion exacta. Lo que no
    # trae es cuerpo, solo titulo y URL.
    ("la_prensa", "https://www.laprensa.hn/sitemapforgoogle.xml", "sitemap_news"),
    ("el_heraldo", "https://www.elheraldo.hn/sitemapforgoogle.xml", "sitemap_news"),
    ("diez", "https://www.diez.hn/sitemapforgoogle.xml", "sitemap_news"),
]

# Quedaron afuera a proposito, todas probadas el 2026-08-19:
#   - tiempo.hn        -> Cloudflare devuelve 403 al /feed/ desde fuera. Puede
#                         que pase desde la EC2; hay que probarlo ahi.
#   - latribuna.hn     -> es WordPress pero el tema intercepta /feed/ y devuelve
#                         la portada en HTML. Su post-sitemap.xml no tiene
#                         namespace news:, o sea sin titulo ni fecha de
#                         publicacion: haria falta scrapear cada nota.
#   - reportarsinmiedo -> /feed/ responde 200 con cuerpo vacio.
#   - hrn.hn, estrategiaynegocios.net -> no resolvieron DNS.


def seed(fecha_corte: datetime | None) -> None:
    engine = get_engine()
    with Session(engine) as session:
        for codigo, nombre in MEDIOS_DIGITALES:
            if session.scalar(select(Medio).where(Medio.codigo == codigo)) is None:
                session.add(Medio(codigo=codigo, nombre=nombre, tipo=TipoMedio.DIGITAL))
                print(f"+ medio digital creado: {codigo} ({nombre})")
        session.flush()

        for codigo, url, tipo in FUENTES:
            medio = session.scalar(select(Medio).where(Medio.codigo == codigo))
            if medio is None:
                # Pasa si no se corrio scripts/seed_medios.py: hch_tv, canal_11 y
                # radio_america vienen de ahi.
                print(f"! medio '{codigo}' no existe, se omite {url}")
                continue
            if session.scalar(select(FuenteWeb).where(FuenteWeb.url == url)) is not None:
                continue
            session.add(
                FuenteWeb(
                    medio_id=medio.id,
                    url=url,
                    tipo=TipoFuente(tipo),
                    fecha_corte=fecha_corte,
                )
            )
            corte = fecha_corte.isoformat() if fecha_corte else "sin corte"
            print(f"  + fuente {tipo}: {url} (corte: {corte})")

        session.commit()
    print("Seed de fuentes de prensa completo.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--corte-horas",
        type=float,
        default=0.0,
        help="ingerir solo lo publicado en las ultimas N horas (default 0 = desde ahora)",
    )
    p.add_argument(
        "--sin-corte",
        action="store_true",
        help="sin fecha de corte: ingiere todo el historial que traiga cada feed",
    )
    a = p.parse_args()
    corte = None if a.sin_corte else datetime.now(timezone.utc) - timedelta(hours=a.corte_horas)
    seed(corte)
