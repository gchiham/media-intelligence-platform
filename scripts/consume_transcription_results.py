"""Corre TranscriptionResultConsumer + TranscriptionFailureConsumer una
pasada cada uno (no es un loop infinito -- pensado para cron, ej. cada
1-2 minutos mientras dure el backlog grande, o on-demand). Ver
docs/INGESTION_DESIGN.md.

Uso: python scripts/consume_transcription_results.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.recordings.repositories import GrabacionRepository, TranscripcionRepository  # noqa: E402
from src.modules.recordings.result_consumer import (  # noqa: E402
    TranscriptionFailureConsumer,
    TranscriptionResultConsumer,
)


def main() -> None:
    if not settings.transcription_done_queue_url:
        raise SystemExit("falta TRANSCRIPTION_DONE_QUEUE_URL en .env")
    if not settings.transcription_dlq_url:
        raise SystemExit("falta TRANSCRIPTION_DLQ_URL en .env")

    s3 = boto3.client("s3", region_name=settings.aws_region)
    sqs = boto3.client("sqs", region_name=settings.aws_region)

    with Session(get_engine()) as session:
        result_consumer = TranscriptionResultConsumer(
            grabaciones=GrabacionRepository(session),
            transcripciones=TranscripcionRepository(session),
            sqs_client=sqs,
            s3_client=s3,
            queue_url=settings.transcription_done_queue_url,
        )
        result = result_consumer.consume_once()
        print(
            f"transcripciones creadas: {result.procesados}, ya existian: {result.omitidos_ya_existian}, "
            f"sin grabacion_id: {result.sin_grabacion_id}"
        )

    with Session(get_engine()) as session:
        failure_consumer = TranscriptionFailureConsumer(
            grabaciones=GrabacionRepository(session), sqs_client=sqs, dlq_url=settings.transcription_dlq_url,
        )
        marcadas = failure_consumer.consume_once()
        print(f"grabaciones marcadas en error: {marcadas}")


if __name__ == "__main__":
    main()
