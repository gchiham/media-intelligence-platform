"""Decide que hacer con un mensaje SQS que fallo, y ejecuta esa decision.

- PermanentPipelineError: reintentar es inutil (audio corrupto, words.json
  invalido, etc.) -- se reenvia a la DLQ de inmediato, con el ErrorContext
  completo adjunto, y se borra de la cola principal. No desperdicia los 3
  intentos del RedrivePolicy en algo que nunca va a funcionar.
- TransientPipelineError: se deja el mensaje en la cola (no se borra) y se
  extiende su VisibilityTimeout segun la politica de backoff -- el respaldo
  final sigue siendo el RedrivePolicy de la cola (maxReceiveCount=3), que lo
  manda solo a la DLQ si sigue fallando.

Ver docs/ERROR_HANDLING.md para el flujo completo.
"""
import json
from typing import Literal

from src.modules.transcription.queue.retry_policy import compute_backoff_seconds
from src.shared.error_context import ErrorContext
from src.shared.errors import PermanentPipelineError, PipelineError
from src.shared.logging_utils import get_logger, log_pipeline_error

logger = get_logger(__name__)

Action = Literal["dlq_immediate", "retry_backoff"]


def handle_failure(
    sqs_client,
    queue_url: str,
    dlq_url: str,
    message: dict,
    original_body: dict,
    error: PipelineError,
    error_context: ErrorContext,
) -> Action:
    log_pipeline_error(logger, error_context)

    if isinstance(error, PermanentPipelineError):
        _forward_to_dlq(sqs_client, dlq_url, original_body, error_context)
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
        return "dlq_immediate"

    backoff = compute_backoff_seconds(error_context.attempt)
    sqs_client.change_message_visibility(
        QueueUrl=queue_url,
        ReceiptHandle=message["ReceiptHandle"],
        VisibilityTimeout=backoff,
    )
    return "retry_backoff"


def _forward_to_dlq(sqs_client, dlq_url: str, original_body: dict, error_context: ErrorContext) -> None:
    envelope = {
        "original_job": original_body,
        "error": error_context.model_dump(),
    }
    sqs_client.send_message(QueueUrl=dlq_url, MessageBody=json.dumps(envelope, ensure_ascii=False))
