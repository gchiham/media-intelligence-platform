"""One-off: la cola SQS ya se purgo. Reencola primero las 3 horas de canal_5
pendientes (21:00-23:00 local del 20-jul), despues el resto del backlog en
estado PROCESANDO, en orden cronologico."""
import uuid

import boto3
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.infrastructure.config import settings
from src.infrastructure.db.engine import get_engine
from src.modules.recordings.models import EstadoGrabacion, Grabacion
from src.modules.recordings.queue_service import QueueService
from src.modules.recordings.repositories import GrabacionRepository

PRIORIDAD_IDS = [
    uuid.UUID("019f82dd-fa4b-78f9-990c-fe439b7b2ad7"),  # canal_5 2026-07-21T03Z
    uuid.UUID("019f8317-b892-7b9d-8d75-8b3a258d9d6b"),  # canal_5 2026-07-21T04Z
    uuid.UUID("019f8347-4358-7009-a3f2-543097bddaba"),  # canal_5 2026-07-21T05Z
]

with Session(get_engine()) as session:
    repo = GrabacionRepository(session)
    service = QueueService(
        grabaciones=repo,
        sqs_client=boto3.client("sqs", region_name=settings.aws_region),
        queue_url=settings.transcription_jobs_queue_url,
        capture_bucket=settings.capture_bucket,
        output_bucket=settings.transcribe_output_bucket,
    )

    prioridad = [repo.get_by_id(i) for i in PRIORIDAD_IDS]
    prioridad = [g for g in prioridad if g is not None]
    print(f"prioridad: {len(prioridad)} de {len(PRIORIDAD_IDS)} encontradas")

    resto = session.scalars(
        select(Grabacion)
        .where(Grabacion.estado == EstadoGrabacion.PROCESANDO, ~Grabacion.id.in_(PRIORIDAD_IDS))
        .order_by(Grabacion.fecha_inicio.asc())
    ).all()
    print(f"resto del backlog: {len(resto)}")

    for g in prioridad + resto:
        service._publish(g)

    print("reencolado terminado")
