"""Excepciones tipadas del pipeline, con una unica distincion que gobierna todo
el manejo de errores: TransientPipelineError (reintentable) vs
PermanentPipelineError (no reintentable). Ver docs/ERROR_HANDLING.md.

`classify_and_wrap` traduce excepciones "crudas" de librerias de terceros
(botocore, openai, ffmpeg via subprocess, json/pydantic) a una de estas dos
categorias, sin que el codigo que llama tenga que conocer los detalles de cada
libreria."""
import json
import subprocess

from pydantic import ValidationError


class PipelineError(Exception):
    """Base de todas las excepciones del pipeline. Nunca se lanza directo --
    siempre una de las dos subclases."""

    def __init__(self, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


class TransientPipelineError(PipelineError):
    """Error temporal: reintentable. Ej: red, timeout, rate limit de un
    proveedor externo, servicio temporalmente inaccesible."""


class PermanentPipelineError(PipelineError):
    """Error permanente: reintentar no va a cambiar el resultado. Ej: audio
    corrupto, JSON/esquema invalido, archivo inexistente."""


_TRANSIENT_BOTOCORE_CODES = {
    "SlowDown",
    "InternalError",
    "RequestTimeout",
    "RequestTimeTooSkewed",
    "ServiceUnavailable",
    "Throttling",
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
}

_PERMANENT_BOTOCORE_CODES = {
    "NoSuchKey",
    "NoSuchBucket",
    "AccessDenied",
    "InvalidObjectState",
    "InvalidArgument",
    # boto3 download_file() hace un HeadObject antes del GetObject -- S3
    # responde HeadObject sobre una key inexistente con code="404", no
    # "NoSuchKey" (eso es solo para GetObject). Verificado contra S3 real.
    "404",
    "403",
}


def _classify_botocore(exc) -> type[PipelineError]:
    code = exc.response.get("Error", {}).get("Code", "")
    if code in _PERMANENT_BOTOCORE_CODES:
        return PermanentPipelineError
    if code in _TRANSIENT_BOTOCORE_CODES:
        return TransientPipelineError
    # Codigo desconocido: se trata como transitorio -- el respaldo real es el
    # RedrivePolicy de la cola (maxReceiveCount), que igual la manda a la DLQ
    # tras N intentos aunque la hayamos clasificado mal. Ver ERROR_HANDLING.md.
    return TransientPipelineError


def _classify_openai(exc) -> type[PipelineError] | None:
    try:
        import openai
    except ImportError:
        return None

    if isinstance(exc, (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)):
        return TransientPipelineError
    if isinstance(exc, (openai.BadRequestError, openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError)):
        return PermanentPipelineError
    return None


def _classify_anthropic(exc) -> type[PipelineError] | None:
    try:
        import anthropic
    except ImportError:
        return None

    if isinstance(exc, (anthropic.RateLimitError, anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.InternalServerError)):
        return TransientPipelineError
    if isinstance(exc, (anthropic.BadRequestError, anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)):
        return PermanentPipelineError
    return None


def classify_and_wrap(
    exc: Exception,
    *,
    module: str,
    default: type[PipelineError] = TransientPipelineError,
) -> PipelineError:
    """Envuelve `exc` en PipelineError, decidiendo Transient vs Permanent segun
    el tipo real de la excepcion original. `module` es solo para el mensaje --
    el llamador todavia arma su propio ErrorContext con job_id/audio_ref."""
    if isinstance(exc, PipelineError):
        return exc

    try:
        from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError, EndpointConnectionError

        if isinstance(exc, (BotoConnectionError, EndpointConnectionError)):
            return TransientPipelineError(f"[{module}] error de red con AWS: {exc}", cause=exc)
        if isinstance(exc, ClientError):
            cls = _classify_botocore(exc)
            return cls(f"[{module}] {exc}", cause=exc)
    except ImportError:
        pass

    openai_cls = _classify_openai(exc)
    if openai_cls is not None:
        return openai_cls(f"[{module}] {exc}", cause=exc)

    anthropic_cls = _classify_anthropic(exc)
    if anthropic_cls is not None:
        return anthropic_cls(f"[{module}] {exc}", cause=exc)

    if isinstance(exc, (json.JSONDecodeError, ValidationError)):
        return PermanentPipelineError(f"[{module}] datos invalidos: {exc}", cause=exc)

    if isinstance(exc, subprocess.CalledProcessError):
        stderr = (exc.stderr or "")
        stderr_text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else str(stderr)
        if "no space left on device" in stderr_text.lower():
            return TransientPipelineError(f"[{module}] disco lleno: {stderr_text[-500:]}", cause=exc)
        return PermanentPipelineError(f"[{module}] ffmpeg fallo (probable archivo danado): {stderr_text[-500:]}", cause=exc)

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TransientPipelineError(f"[{module}] {exc}", cause=exc)

    return default(f"[{module}] {exc}", cause=exc)
