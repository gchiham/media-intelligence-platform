"""Consumidores livianos (CPU, sin GPU) que cierran el circulo entre chepita
y Postgres -- chepita nunca tiene credenciales de DB (docs/INGESTION_DESIGN.md,
punto 2), asi que esto vive en el backend, no en la instancia GPU.

TranscriptionResultConsumer: consume `media-intel-transcription-done` (la
publica el propio worker de chepita despues de subir el resultado a S3 y
borrar el mensaje original), descarga `_words.json` de S3, crea la
Transcripcion y marca la Grabacion como PROCESADA.

TranscriptionFailureConsumer: consume la DLQ existente
(`media-intel-transcription-jobs-dlq`) y marca la Grabacion correspondiente
como ERROR. Soporta los dos formatos de mensaje que puede tener la DLQ (ver
docs/ERROR_HANDLING.md): el envelope completo `{"original_job", "error"}"`
cuando el worker reenvia un fallo permanente, o el body crudo del job cuando
SQS mueve el mensaje solo tras agotar el RedrivePolicy (sin envelope,
sin detalle de error disponible).

Idempotencia: ambos son seguros ante entrega duplicada (SQS es
at-least-once) -- el resultado consumer no duplica Transcripcion
(`grabacion_id` es unique) y ambos solo borran el mensaje despues de que el
efecto en Postgres quedo confirmado (commit), nunca antes.
"""
import json
from dataclasses import dataclass

from src.modules.recordings.models import EstadoGrabacion, Transcripcion
from src.modules.recordings.repositories import GrabacionRepository, TranscripcionRepository
from src.shared.logging_utils import get_logger

logger = get_logger("transcription_result_consumer")


@dataclass
class ConsumeResult:
    procesados: int
    omitidos_ya_existian: int
    sin_grabacion_id: int


def _download_json(s3_client, s3_uri: str) -> list[dict]:
    bucket, key = s3_uri.replace("s3://", "").split("/", 1)
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read())


class TranscriptionResultConsumer:
    def __init__(
        self,
        grabaciones: GrabacionRepository,
        transcripciones: TranscripcionRepository,
        sqs_client,
        s3_client,
        queue_url: str,
        provider_name: str = "faster-whisper-small",
        max_messages: int = 10,
        max_receives: int = 200,
    ):
        self._grabaciones = grabaciones
        self._transcripciones = transcripciones
        self._sqs = sqs_client
        self._s3 = s3_client
        self._queue_url = queue_url
        self._provider_name = provider_name
        self._max_messages = max_messages
        # Tope duro de llamadas receive_message por corrida -- a max_messages=10
        # esto es hasta 2000 mensajes por tick de cron, mas que suficiente para
        # el volumen actual. Es una red de seguridad contra un loop infinito si
        # SQS alguna vez devolviera mensajes sin llegar a vaciarse (no deberia
        # pasar en uso normal), no un limite pensado para alcanzarse.
        self._max_receives = max_receives

    def consume_once(self) -> ConsumeResult:
        """Drena todos los mensajes disponibles ahora mismo (loop de
        recibos de a lo sumo `max_messages` -- tope duro de SQS por
        llamada -- hasta que un recibo vuelve vacio), pensado para correr
        como cron/script, no como servicio persistente, mientras el
        volumen no lo justifique.

        Antes esto hacia un solo receive_message y paraba ahi -- con
        `max_messages=10` (tope de SQS) eso limitaba el ingest a 10
        grabaciones por tick de cron sin importar cuantos workers de
        chepita estuvieran produciendo resultados mas rapido."""
        procesados = 0
        omitidos = 0
        sin_id = 0

        for _ in range(self._max_receives):
            resp = self._sqs.receive_message(
                QueueUrl=self._queue_url, MaxNumberOfMessages=self._max_messages, WaitTimeSeconds=5,
            )
            messages = resp.get("Messages", [])
            if not messages:
                break
            for msg in messages:
                body = json.loads(msg["Body"])
                grabacion_id = body.get("grabacion_id")
                if not grabacion_id:
                    sin_id += 1
                    logger.error("mensaje sin grabacion_id, se descarta", extra={"extra_fields": {"body": body}})
                    self._sqs.delete_message(QueueUrl=self._queue_url, ReceiptHandle=msg["ReceiptHandle"])
                    continue

                if self._transcripciones.get_by_grabacion_id(grabacion_id) is not None:
                    omitidos += 1
                    self._sqs.delete_message(QueueUrl=self._queue_url, ReceiptHandle=msg["ReceiptHandle"])
                    continue

                words = _download_json(self._s3, body["words_json_s3_uri"])

                grabacion = self._grabaciones.get_by_id(grabacion_id)
                if grabacion is None:
                    logger.error(
                        "grabacion_id del mensaje no existe en Postgres",
                        extra={"extra_fields": {"grabacion_id": str(grabacion_id)}},
                    )
                    self._sqs.delete_message(QueueUrl=self._queue_url, ReceiptHandle=msg["ReceiptHandle"])
                    continue

                transcripcion = Transcripcion(
                    grabacion_id=grabacion.id,
                    texto_completo=" ".join(w["word"] for w in words),
                    segmentos={"words": words},
                    proveedor=self._provider_name,
                )
                self._transcripciones.add(transcripcion)
                grabacion.estado = EstadoGrabacion.PROCESADA
                self._grabaciones.commit()

                self._sqs.delete_message(QueueUrl=self._queue_url, ReceiptHandle=msg["ReceiptHandle"])
                procesados += 1

        return ConsumeResult(procesados=procesados, omitidos_ya_existian=omitidos, sin_grabacion_id=sin_id)


class TranscriptionFailureConsumer:
    def __init__(self, grabaciones: GrabacionRepository, sqs_client, dlq_url: str, max_messages: int = 10):
        self._grabaciones = grabaciones
        self._sqs = sqs_client
        self._dlq_url = dlq_url
        self._max_messages = max_messages

    def consume_once(self) -> int:
        marcadas = 0
        resp = self._sqs.receive_message(
            QueueUrl=self._dlq_url, MaxNumberOfMessages=self._max_messages, WaitTimeSeconds=5,
        )
        for msg in resp.get("Messages", []):
            body = json.loads(msg["Body"])
            original_job = body.get("original_job", body)
            error_message = (
                json.dumps(body["error"], ensure_ascii=False)
                if "error" in body
                else "reintentos agotados (RedrivePolicy automatico, sin detalle de error disponible)"
            )
            grabacion_id = original_job.get("grabacion_id")

            if grabacion_id:
                grabacion = self._grabaciones.get_by_id(grabacion_id)
                if grabacion is not None:
                    grabacion.estado = EstadoGrabacion.ERROR
                    grabacion.error_mensaje = error_message
                    self._grabaciones.commit()
                    marcadas += 1
                else:
                    logger.error(
                        "grabacion_id de la DLQ no existe en Postgres",
                        extra={"extra_fields": {"grabacion_id": str(grabacion_id)}},
                    )
            else:
                logger.error("mensaje de DLQ sin grabacion_id, no se puede marcar", extra={"extra_fields": {"body": body}})

            self._sqs.delete_message(QueueUrl=self._dlq_url, ReceiptHandle=msg["ReceiptHandle"])

        return marcadas
