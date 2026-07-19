"""Contexto estructurado de un error del pipeline -- los campos exactos que se
piden registrar: job_id, audio original, error completo, stack trace, fecha,
numero de intento, modulo que fallo. Se usa tanto para logging estructurado
como para el envelope que se manda a la DLQ."""
import traceback
from datetime import datetime, timezone

from pydantic import BaseModel


class ErrorContext(BaseModel):
    job_id: str
    module: str
    audio_ref: str | None
    error_type: str
    error_message: str
    stack_trace: str
    attempt: int
    occurred_at: str


def build_error_context(
    error: Exception,
    *,
    module: str,
    job_id: str,
    audio_ref: str | None = None,
    attempt: int,
) -> ErrorContext:
    return ErrorContext(
        job_id=job_id,
        module=module,
        audio_ref=audio_ref,
        error_type=type(error).__name__,
        error_message=str(error),
        stack_trace="".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
        attempt=attempt,
        occurred_at=datetime.now(timezone.utc).isoformat(),
    )
