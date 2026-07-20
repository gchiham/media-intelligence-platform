"""Siembra los medios reales (fuente de verdad: config/stations.json del repo mediaCAP)
y un programa generico por medio (mediaCAP no distingue programas por horario todavia,
solo graba continuo por hora). Idempotente: se puede correr varias veces sin duplicar.

Uso: python scripts/seed_medios.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.media.models import Medio, Programa, TipoMedio  # noqa: E402

STATIONS = [
    ("xy_hrn", "XY HRN", "radio"),
    ("xy_tgu", "XY TGU", "radio"),
    ("xy_sps", "XY SPS", "radio"),
    ("radio_satelite", "Radio Satelite", "radio"),
    ("fm_941", "94.1 FM", "radio"),
    ("suave_fm", "Suave FM", "radio"),
    ("radio_america", "Radio America", "radio"),
    ("radio_globo", "Radio Globo", "radio"),
    ("radio_el_patio", "Radio El Patio", "radio"),
    ("hch_tv", "HCH TV", "tv"),
    ("teleceiba", "Teleceiba", "tv"),
    ("radio_choluteca", "Radio Choluteca", "radio"),
    ("canal_11", "Canal 11", "tv"),
    # No estan en stations.json (posiblemente descontinuados o pendientes de agregar a la
    # config activa de mediaCAP), pero tienen grabaciones reales en el bucket S3.
    ("hch_radio", "HCH Radio", "radio"),
    ("canal_5", "Canal 5", "tv"),
    ("canal_6", "Canal 6", "tv"),
    ("radio_valle", "Radio Valle", "radio"),
    # Agregadas 2026-07-20 (docs/INGESTION_DESIGN.md): carpetas reales en
    # s3://mediadev-recordings/ sin Medio sembrado todavia -- DiscoveryService
    # las ignora (y las reporta) mientras no esten aca. Clasificacion
    # radio/tv por nombre, no por config/stations.json -- revisar si esta mal.
    ("suave_fm_teg", "Suave FM Tegucigalpa", "radio"),
    ("super_100", "Super 100", "radio"),
    ("tnh", "TNH", "tv"),
    ("tsi", "TSI", "radio"),
]


def seed() -> None:
    engine = get_engine()
    with Session(engine) as session:
        for codigo, nombre, tipo in STATIONS:
            medio = session.scalar(select(Medio).where(Medio.codigo == codigo))
            if medio is None:
                medio = Medio(codigo=codigo, nombre=nombre, tipo=TipoMedio(tipo))
                session.add(medio)
                session.flush()
                print(f"+ medio creado: {codigo} ({nombre})")

            programa = session.scalar(select(Programa).where(Programa.medio_id == medio.id))
            if programa is None:
                programa = Programa(medio_id=medio.id, nombre="General")
                session.add(programa)
                print(f"  + programa 'General' creado para {codigo}")

        session.commit()
    print("Seed de medios completo.")


if __name__ == "__main__":
    seed()
