"""Corre DiscoveryService una vez: escanea settings.capture_bucket y crea
Grabacion(estado=PENDIENTE) para cada archivo nuevo. Idempotente -- correrlo
de nuevo no duplica nada. Requiere haber corrido seed_medios.py
antes (si no, los archivos de estaciones sin Medio se listan y se ignoran).

Uso: python scripts/discover_grabaciones.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.media.repositories import MedioRepository, ProgramaRepository  # noqa: E402
from src.modules.recordings.discovery import DiscoveryService  # noqa: E402
from src.modules.recordings.repositories import GrabacionRepository  # noqa: E402


def main() -> None:
    with Session(get_engine()) as session:
        service = DiscoveryService(
            grabaciones=GrabacionRepository(session),
            medios=MedioRepository(session),
            programas=ProgramaRepository(session),
            s3_client=boto3.client("s3", region_name=settings.aws_region),
            bucket=settings.capture_bucket,
        )
        result = service.discover()

        print(f"creadas: {result.creadas}")
        print(f"ya existian: {result.ya_existian}")
        print(f"ignoradas (no reconocidas): {result.ignoradas_no_reconocidas}")
        if result.estaciones_sin_medio:
            print(f"ATENCION -- estaciones sin Medio (corre seed_medios.py): "
                  f"{sorted(result.estaciones_sin_medio)}")


if __name__ == "__main__":
    main()
