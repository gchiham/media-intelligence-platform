"""Corre CoverageDiscoveryService una vez: lee recording_coverage (DB externa
del sistema capturador, solo lectura) y crea Grabacion(estado=PENDIENTE) para
cada fila audio/uploaded nueva. Idempotente -- correrlo de nuevo no duplica
nada. Requiere DATABASE_URL_COVERAGE en .env y haber corrido seed_medios.py
antes (si no, las estaciones sin Medio se listan y se ignoran, igual que en
discover_grabaciones.py).

Corre en paralelo a discover_grabaciones.py por ahora -- no lo reemplaza.

Uso: python scripts/discover_grabaciones_coverage.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_coverage_session, get_engine  # noqa: E402
from src.modules.media.repositories import MedioRepository, ProgramaRepository  # noqa: E402
from src.modules.recordings.coverage_discovery import CoverageDiscoveryService  # noqa: E402
from src.modules.recordings.repositories import GrabacionRepository  # noqa: E402


def main() -> None:
    with Session(get_engine()) as session, get_coverage_session() as coverage_session:
        service = CoverageDiscoveryService(
            grabaciones=GrabacionRepository(session),
            medios=MedioRepository(session),
            programas=ProgramaRepository(session),
            coverage_session=coverage_session,
        )
        result = service.discover()

        print(f"creadas: {result.creadas}")
        print(f"ya existian: {result.ya_existian}")
        if result.estaciones_sin_medio:
            print(f"ATENCION -- estaciones sin Medio (corre seed_medios.py): "
                  f"{sorted(result.estaciones_sin_medio)}")


if __name__ == "__main__":
    main()
