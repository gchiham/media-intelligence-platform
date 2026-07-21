"""Corre QueueService una vez: toma Grabacion(estado=PENDIENTE) y las publica
a la cola SQS de transcripcion para que chepita las procese. Idempotente --
marca cada una como PROCESANDO en la misma pasada, asi que correrlo de nuevo
no re-encola lo que ya se encolo.

Uso: python scripts/enqueue_transcriptions.py [--limit N] [--medio CODIGO]
     [--fecha YYYY-MM-DD | --fecha-desde YYYY-MM-DD --fecha-hasta YYYY-MM-DD]

--medio filtra por el codigo del Medio (p.ej. canal_5), usando el Programa
catch-all sembrado por seed_medios.py -- si se omite, aplica a todos los
medios. --fecha filtra un solo dia (UTC); --fecha-desde/--fecha-hasta filtran
un rango (fecha_inicio >= desde AND < hasta, UTC, hasta exclusivo). Sin
ninguno de los tres, se encola todo lo pendiente sin importar la fecha.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.media.repositories import MedioRepository, ProgramaRepository  # noqa: E402
from src.modules.recordings.queue_service import QueueService  # noqa: E402
from src.modules.recordings.repositories import GrabacionRepository  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--medio", type=str, default=None, help="codigo del Medio, p.ej. canal_5")
    parser.add_argument("--fecha", type=str, default=None, help="YYYY-MM-DD (UTC), dia unico")
    parser.add_argument("--fecha-desde", type=str, default=None, help="YYYY-MM-DD (UTC), inclusive")
    parser.add_argument("--fecha-hasta", type=str, default=None, help="YYYY-MM-DD (UTC), exclusivo")
    args = parser.parse_args()

    if args.fecha and (args.fecha_desde or args.fecha_hasta):
        raise SystemExit("usa --fecha O --fecha-desde/--fecha-hasta, no ambos")

    if not settings.transcription_jobs_queue_url:
        raise SystemExit("falta TRANSCRIPTION_JOBS_QUEUE_URL en .env")

    with Session(get_engine()) as session:
        programa_id = None
        if args.medio:
            medio = MedioRepository(session).get_by_codigo(args.medio)
            if medio is None:
                raise SystemExit(f"no existe un Medio con codigo '{args.medio}'")
            programa = ProgramaRepository(session).get_first_by_medio_id(medio.id)
            if programa is None:
                raise SystemExit(f"Medio '{args.medio}' no tiene Programa sembrado")
            programa_id = programa.id

        fecha_desde = fecha_hasta = None
        if args.fecha:
            dia = datetime.strptime(args.fecha, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            fecha_desde = dia
            fecha_hasta = dia + timedelta(days=1)
        elif args.fecha_desde or args.fecha_hasta:
            if args.fecha_desde:
                fecha_desde = datetime.strptime(args.fecha_desde, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if args.fecha_hasta:
                fecha_hasta = datetime.strptime(args.fecha_hasta, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ) + timedelta(days=1)

        service = QueueService(
            grabaciones=GrabacionRepository(session),
            sqs_client=boto3.client("sqs", region_name=settings.aws_region),
            queue_url=settings.transcription_jobs_queue_url,
            capture_bucket=settings.capture_bucket,
            output_bucket=settings.transcribe_output_bucket,
        )
        result = service.enqueue_pending(
            limit=args.limit,
            programa_id=programa_id,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
        )
        print(f"encoladas: {result.encoladas}")


if __name__ == "__main__":
    main()
