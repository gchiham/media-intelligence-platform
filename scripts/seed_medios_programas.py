"""Siembra Medio + un Programa 'catch-all' por cada estacion detectada en el
S3 de captura (`settings.capture_bucket`) -- bloqueante para DiscoveryService,
que necesita `programa_id` para poder crear una Grabacion (ver
docs/INGESTION_DESIGN.md).

La lista de estaciones/tipo de medio de abajo se tomo de los nombres de
carpeta reales en S3 (`aws s3 ls s3://mediadev-recordings/`) el 2026-07-20 --
no hay todavia una fuente de verdad formal en este repo (el comentario en
Medio.codigo referencia `config/stations.json` del repo mediaCAP, externo a
este). La clasificacion radio/tv es una inferencia por nombre, no una fuente
oficial -- revisar y corregir a mano si algo esta mal antes de confiar en
reportes que separen por tipo.

Idempotente: usa `codigo`/`nombre` unique de Medio para no duplicar si se
corre dos veces.

Uso: python scripts/seed_medios_programas.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.media.models import Medio, Programa, TipoMedio  # noqa: E402
from src.modules.media.repositories import MedioRepository, ProgramaRepository  # noqa: E402

# (codigo, nombre legible, tipo)
ESTACIONES = [
    ("canal_11", "Canal 11", TipoMedio.TV),
    ("canal_5", "Canal 5", TipoMedio.TV),
    ("canal_6", "Canal 6", TipoMedio.TV),
    ("fm_941", "FM 94.1", TipoMedio.RADIO),
    ("hch_radio", "HCH Radio", TipoMedio.RADIO),
    ("hch_tv", "HCH TV", TipoMedio.TV),
    ("radio_america", "Radio America", TipoMedio.RADIO),
    ("radio_choluteca", "Radio Choluteca", TipoMedio.RADIO),
    ("radio_el_patio", "Radio El Patio", TipoMedio.RADIO),
    ("radio_globo", "Radio Globo", TipoMedio.RADIO),
    ("radio_satelite", "Radio Satelite", TipoMedio.RADIO),
    ("radio_valle", "Radio Valle", TipoMedio.RADIO),
    ("suave_fm", "Suave FM", TipoMedio.RADIO),
    ("suave_fm_teg", "Suave FM Tegucigalpa", TipoMedio.RADIO),
    ("super_100", "Super 100", TipoMedio.RADIO),
    ("teleceiba", "Teleceiba", TipoMedio.TV),
    ("tnh", "TNH", TipoMedio.TV),
    ("tsi", "TSI", TipoMedio.RADIO),
]


def main() -> None:
    with Session(get_engine()) as session:
        medios = MedioRepository(session)
        programas = ProgramaRepository(session)

        creados_medio = 0
        creados_programa = 0
        for codigo, nombre, tipo in ESTACIONES:
            medio = medios.get_by_codigo(codigo)
            if medio is None:
                medio = Medio(codigo=codigo, nombre=nombre, tipo=tipo)
                medios.add(medio)
                session.flush()
                creados_medio += 1

            if programas.get_first_by_medio_id(medio.id) is None:
                programas.add(
                    Programa(medio_id=medio.id, nombre="Transmision continua", horario=None)
                )
                creados_programa += 1

        session.commit()
        print(
            f"medios nuevos: {creados_medio}, programas nuevos: {creados_programa}, "
            f"total estaciones consideradas: {len(ESTACIONES)}"
        )


if __name__ == "__main__":
    main()
