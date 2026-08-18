"""One-off: la cola SQS ya se purgo. Reencola todas las Grabacion en estado
'procesando' (ya se habian encolado antes, pero la purga se llevo sus
mensajes) -- primero las de hoy (2026-07-20 local, UTC-6), despues el resto
del backlog en orden cronologico. No usa QueueService.enqueue_pending porque
ese metodo solo toma PENDIENTE; aqui todo ya esta en PROCESANDO."""
from datetime import datetime, timezone

import boto3
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.infrastructure.config import settings
from src.infrastructure.db.engine import get_engine
from src.modules.recordings.models import EstadoGrabacion, Grabacion
from src.modules.recordings.queue_service import QueueService
from src.modules.recordings.repositories import GrabacionRepository

HOY_DESDE = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)  # 2026-07-20 00:00 Honduras (UTC-6)
HOY_HASTA = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)

with Session(get_engine()) as session:
    repo = GrabacionRepository(session)
    service = QueueService(
        grabaciones=repo,
        sqs_client=boto3.client("sqs", region_name=settings.aws_region),
        queue_url=settings.transcription_jobs_queue_url,
        capture_bucket=settings.capture_bucket,
        output_bucket=settings.transcribe_output_bucket,
    )

    hoy = session.scalars(
        select(Grabacion)
        .where(
            Grabacion.estado == EstadoGrabacion.PROCESANDO,
            Grabacion.fecha_inicio >= HOY_DESDE,
            Grabacion.fecha_inicio < HOY_HASTA,
        )
        .order_by(Grabacion.fecha_inicio.asc())
    ).all()

    resto = session.scalars(
        select(Grabacion)
        .where(
            Grabacion.estado == EstadoGrabacion.PROCESANDO,
            ~((Grabacion.fecha_inicio >= HOY_DESDE) & (Grabacion.fecha_inicio < HOY_HASTA)),
        )
        .order_by(Grabacion.fecha_inicio.asc())
    ).all()

    print(f"hoy: {len(hoy)} | resto: {len(resto)}")

    for g in hoy + resto:
        service._publish(g)

    print("reencolado terminado")
