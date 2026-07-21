"""QueueService: toma Grabaciones en PENDIENTE y las encola para que chepita
las transcriba -- un mensaje SQS por Grabacion, con `grabacion_id` incluido
(a diferencia del formato de job anterior, que solo llevaba `station`, sin
forma de mapear un resultado de vuelta a una fila de Postgres). Ver
docs/INGESTION_DESIGN.md.

Idempotencia: solo lee `estado=PENDIENTE` y lo pasa a `PROCESANDO` en la
misma pasada -- una Grabacion ya encolada no vuelve a aparecer en la
siguiente corrida, aunque el script se corra de nuevo antes de que chepita
termine.
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from src.modules.recordings.models import EstadoGrabacion, Grabacion
from src.modules.recordings.repositories import GrabacionRepository
from src.shared.logging_utils import get_logger

logger = get_logger("queue_service")


def _station_from_s3_key(s3_key: str) -> str:
    return s3_key.split("/", 1)[0]


@dataclass
class EnqueueResult:
    encoladas: int


class QueueService:
    def __init__(
        self,
        grabaciones: GrabacionRepository,
        sqs_client,
        queue_url: str,
        capture_bucket: str,
        output_bucket: str,
    ):
        self._grabaciones = grabaciones
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._capture_bucket = capture_bucket
        self._output_bucket = output_bucket

    def enqueue_pending(
        self,
        limit: int = 500,
        programa_id: uuid.UUID | None = None,
        fecha_desde: datetime | None = None,
        fecha_hasta: datetime | None = None,
    ) -> EnqueueResult:
        pendientes = self._grabaciones.list_pendientes(
            limit=limit,
            programa_id=programa_id,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
        )
        encoladas = 0
        for grabacion in pendientes:
            self._publish(grabacion)
            grabacion.estado = EstadoGrabacion.PROCESANDO
            encoladas += 1
        if encoladas:
            self._grabaciones.commit()
        logger.info("grabaciones encoladas", extra={"extra_fields": {"count": encoladas}})
        return EnqueueResult(encoladas=encoladas)

    def _publish(self, grabacion: Grabacion) -> None:
        station = _station_from_s3_key(grabacion.s3_key)
        output_key = f"transcripts/{station}/{grabacion.fecha_inicio.strftime('%Y-%m-%dT%HZ')}"
        body = {
            "grabacion_id": str(grabacion.id),
            "station": station,
            "s3_input": f"s3://{self._capture_bucket}/{grabacion.s3_key}",
            "s3_output_prefix": f"s3://{self._output_bucket}/{output_key}",
        }
        self._sqs.send_message(QueueUrl=self._queue_url, MessageBody=json.dumps(body))
