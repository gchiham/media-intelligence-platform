"""Corre QueueService una vez: toma Grabacion(estado=PENDIENTE) y las publica
a la cola SQS de transcripcion para que chepita las procese. Idempotente --
marca cada una como PROCESANDO en la misma pasada, asi que correrlo de nuevo
no re-encola lo que ya se encolo.

Uso: python scripts/enqueue_transcriptions.py [--limit N]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.recordings.queue_service import QueueService  # noqa: E402
from src.modules.recordings.repositories import GrabacionRepository  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    if not settings.transcription_jobs_queue_url:
        raise SystemExit("falta TRANSCRIPTION_JOBS_QUEUE_URL en .env")

    with Session(get_engine()) as session:
        service = QueueService(
            grabaciones=GrabacionRepository(session),
            sqs_client=boto3.client("sqs", region_name=settings.aws_region),
            queue_url=settings.transcription_jobs_queue_url,
            capture_bucket=settings.capture_bucket,
            output_bucket=settings.transcribe_output_bucket,
        )
        result = service.enqueue_pending(limit=args.limit)
        print(f"encoladas: {result.encoladas}")


if __name__ == "__main__":
    main()
